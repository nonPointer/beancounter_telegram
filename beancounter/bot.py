"""Bot composition and command handlers, started by the root main.py entry point."""

from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from .dispatch import Dispatcher
from pathlib import Path
from .settings import Settings
from .state_store import StateStore
from datetime import datetime
import pytz
import re
import requests
import signal
import sys
import threading
import time
from datetime import timedelta
from .bot_utils import (
    ACCOUNT_TYPE_MAP, AccountMatchError, BALANCE_TOLERANCE, DRAFT_TTL_SECONDS, FILE_PATH, HTTP,
    MAX_BEANCOUNT_RETRIES, TELEGRAM_MESSAGE_LIMIT, _capped_code_block, _code_block,
    _friendly_header, _is_account_error, _scrub, _utf16_len, display_width,
    extract_all_directive_blocks, extract_json_object, extract_last_directive_block,
    format_query_result, jinja2, log, parse_natural_date,
)
from .bot_utils import LedgerValidationError

from .entries import EntryMixin
from .ledger import LedgerMixin
from .llm import LLMMixin
from .drafts import DraftMixin
from .telegram_api import TelegramMixin


class Bot(EntryMixin, LedgerMixin, LLMMixin, DraftMixin, TelegramMixin):
    def __init__(self, debug: bool = False, settings: Settings | dict | None = None,
                 state_path: str | None = None):
        self.settings = Settings(settings) if isinstance(settings, dict) else settings or Settings.load()
        if not self.settings.ALLOWED_CHATS:
            raise ValueError(
                "CHAT_ID is required: an empty value would let anyone who finds the bot read "
                "the ledger and commit to the repo. Set it in config.json (comma-separated for "
                "multiple chats). Find yours via https://api.telegram.org/bot<TOKEN>/getUpdates"
            )
        self.state = StateStore(state_path or self.settings.STATE_PATH, self.settings.identity)
        self.update_id = self.state.get("offset", 0)
        self.debug = debug
        self.stop = threading.Event()
        self._poll_failures = 0
        self.timezone = pytz.timezone(self.settings.TIMEZONE)
        self.api_base = "https://api.telegram.org/bot{}".format(self.settings.TELEGRAM_BOT_TOKEN)
        saved = self.state.get("drafts", {})
        self.pending_llm_entries = saved.get("pending", {})
        self.pending_llm_entries.update(saved.get("inflight", {}))
        for pending in self.pending_llm_entries.values():
            if pending.pop("rechecking", False):
                pending["auto_confirm"] = False
        self._inflight = {}
        self.pending_llm_id = saved.get("counter", 0)
        self.pending_decline_reasons = {int(k): v for k, v in saved.get("feedback", {}).items()}
        self._pending_lock = threading.Lock()
        self.dispatcher = Dispatcher(self.settings.WORKERS, self.settings.QUEUE_SIZE)
        self._handler_context = threading.local()
        self._closed = False
        self.timezone = pytz.timezone(saved.get("timezone", self.settings.TIMEZONE))
        self._accounts_cache = {"accounts": None, "currencies": {}, "comments": {}, "ts": 0, "sha_map": None}
        self._accounts_cache_lock = threading.Lock()
        self._file_etag_cache = {}  # file_path -> {"etag": str, "content": str, "sha": str}
        # Parsed ledger cached by repo tree sha; reused until any .bean file changes.
        self._ledger_cache = {"tree_sha": None, "entries": None, "options_map": None}
        self._ledger_cache_lock = threading.Lock()
        self._snapshot_cache = (None, None)
        self._account_files = {}
        self._snapshot_lock = threading.RLock()
        self._accounts_refresh_lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._downloads = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ledger-download")
        self.llm_enabled = bool(self.settings.LLM_BACKENDS)

    def is_authorized(self, chat_id) -> bool:
        return str(chat_id) in self.settings.ALLOWED_CHATS

    def handle_photo_message(self, message):
        msg = message["message"]
        chat_id = msg["chat"]["id"]
        caption = msg.get("caption", "").strip()

        def reply(text: str):
            self.send_message(chat_id, text)

        if not self.is_authorized(chat_id):
            log(f"Ignoring photo from unauthorized chat_id {chat_id}.")
            return

        if not self.llm_enabled:
            reply(self.llm_unavailable_message())
            return

        accounts = self.parse_accounts()
        if not accounts:
            reply("No accounts available. Please check GitHub account parsing first.")
            return

        dt = datetime.now(self.timezone)
        date_str = dt.strftime('%Y-%m-%d')
        datetime_str = dt.isoformat(timespec='seconds')

        # Use highest-resolution photo
        file_id = max(msg["photo"], key=lambda p: p.get("file_size", 0))["file_id"]

        image_bytes = self.get_telegram_file_bytes(file_id)
        if not image_bytes:
            reply("Failed to download the image.")
            return

        # Route: caption containing invest/cfd/stocksisa/isa keywords → investment order
        _invest_keywords = {'invest', 'cfd', 'stocksisa', 'isa'}
        _caption_words = set(caption.lower().split())
        is_invest = bool(_caption_words & _invest_keywords)

        try:
            if is_invest:
                log(f"Processing investment order screenshot (caption: {caption!r})")
                appendix = self.call_openai_vision_invest(image_bytes, accounts, date_str, caption, datetime_str)
                draft_label, user_input = "Investment order draft", caption or "(investment order screenshot)"
                commit_prefix = 'Add investment entry by Telegram Bot\n\n'
            else:
                log(f"Processing expense screenshot (caption: {caption!r})")
                appendix = self.call_openai_vision_expense(image_bytes, accounts, date_str, caption, datetime_str)
                draft_label, user_input = "Expense screenshot draft", caption or "(expense screenshot)"
                commit_prefix = 'Add entry by Telegram Bot\n\n'

            if caption:
                appendix = self.insert_prompt_metadata(appendix, caption)
            commit_message = self.add_non_pnl_accounts_to_commit_message(commit_prefix, appendix)

            pending_id = self.next_pending_id()
            with self._pending_lock:
                self.pending_llm_entries[pending_id] = self._make_pending_entry(
                    chat_id, appendix, commit_message, user_input, date_str
                )
                self.pending_llm_entries[pending_id]["photo_file_id"] = file_id

            log(f"{draft_label}:\n" + appendix)
            self.send_draft_for_review(chat_id, f"{draft_label}:", appendix, pending_id)
        except Exception as e:
            log(f"Photo processing failed: {e}")
            reply(str(e) if isinstance(e, LedgerValidationError) else f"Failed to process screenshot: {e}")

    def handle_undo(self, chat_id: int):
        f = self.github_download_file()
        if not f:
            self.send_message(chat_id, "Failed to download main.bean from GitHub.")
            return
        result = extract_last_directive_block(f["content"])
        if result is None:
            self.send_message(chat_id, "main.bean 中没有找到任何指令。")
            return
        directive_text, new_content = result
        header_line = directive_text.splitlines()[0]
        quoted = re.findall(r'"((?:\\.|[^"\\])*)"', header_line)
        if len(quoted) >= 2:
            description = f"{quoted[0]} {quoted[1]}"
        elif len(quoted) == 1:
            description = quoted[0]
        else:
            description = header_line
        commit_message = f"Revert: {description}"
        pending_id = self.next_pending_id()
        with self._pending_lock:
            self.pending_llm_entries[pending_id] = {
                "kind": "undo",
                "chat_id": chat_id,
                "transaction_text": directive_text,
                "new_content": new_content,
                "file_sha": f["sha"],
                "commit_message": commit_message,
                "created_at": time.time(),
            }
            self._save_pending_locked()
        self.send_message(
            chat_id,
            f"撤回最后一条指令？\n{_code_block(directive_text)}",
            reply_markup=self.build_undo_buttons(pending_id),
            parse_mode="HTML",
        )

    def handle_last(self, chat_id: int, count: int = 5):
        count = min(count, 50)
        f = self.github_download_file()
        if not f:
            self.send_message(chat_id, "Failed to download main.bean from GitHub.")
            return
        blocks = extract_all_directive_blocks(f["content"])
        if not blocks:
            self.send_message(chat_id, "main.bean 中没有找到任何记录。")
            return
        last_blocks = blocks[-count:]
        text = "\n\n".join(block_text for _, block_text in last_blocks)
        prefix = f"最近 {len(last_blocks)} 条记录：\n"
        notice = "\n（内容过长，已截断显示）"
        block, truncated = _capped_code_block(
            text, TELEGRAM_MESSAGE_LIMIT - _utf16_len(prefix) - _utf16_len(notice))
        msg = prefix + block + (notice if truncated else "")
        self.send_message(
            chat_id,
            msg,
            parse_mode="HTML",
        )

    def handle_today(self, chat_id: int):
        f = self.github_download_file()
        if not f:
            self.send_message(chat_id, "Failed to download main.bean from GitHub.")
            return
        today = datetime.now(self.timezone).strftime('%Y-%m-%d')
        blocks = extract_all_directive_blocks(f["content"])
        today_blocks = [(d, t) for d, t in blocks if d == today]
        if not today_blocks:
            self.send_message(chat_id, f"今天（{today}）没有记录。")
            return
        text = "\n\n".join(block_text for _, block_text in today_blocks)
        prefix = f"今天（{today}）共 {len(today_blocks)} 条记录：\n"
        notice = "\n（内容过长，已截断显示）"
        block, truncated = _capped_code_block(
            text, TELEGRAM_MESSAGE_LIMIT - _utf16_len(prefix) - _utf16_len(notice))
        self.send_message(
            chat_id,
            prefix + block + (notice if truncated else ""),
            parse_mode="HTML",
        )

    def handle_message(self, message):
        text = message["message"]["text"]
        chat_id = message["message"]["chat"]["id"]

        def reply(text: str):
            self.send_message(chat_id, text)

        if not self.is_authorized(chat_id):
            log(f"Ignoring message from unauthorized chat_id {chat_id}.")
            return

        dt = datetime.now(self.timezone)
        time_str = dt.strftime('%H:%M')
        datetime_str = dt.isoformat(timespec='seconds')

        # Read a pending decline/feedback reason from the RAW incoming message
        # text BEFORE any date parsing or directive detection. A reason that
        # begins with a recognised date keyword (e.g. "昨天买的，不是今天")
        # would otherwise be stripped/mangled by parse_natural_date before the
        # recheck handler sees it, corrupting (or emptying) the reason.
        with self._pending_lock:
            pending_reason_id = self.pending_decline_reasons.get(chat_id)
        if pending_reason_id is not None:
            reason_text = text.strip()
            if not reason_text or reason_text.startswith('/'):
                reply("Please send a non-command reason text, or tap discard.")
                return

            with self._pending_lock:
                self.pending_decline_reasons.pop(chat_id, None)
            log(f"Decline reason received: {reason_text}")
            self.run_recheck(chat_id, pending_reason_id, decline_reason=reason_text)
            return

        # Detect beancount directive commands from raw text BEFORE natural
        # language date parsing.  parsedatetime can false-positive on numbers
        # embedded in command args (e.g. "balance acc -41.1 GBP" → year 2042),
        # consuming the entire line and breaking command dispatch.
        _directive_commands = {'open', 'close', 'balance', 'pad'}
        _first_word = text.strip().split()[0].lower() if text.strip() else ''
        if _first_word in _directive_commands:
            date_str = dt.strftime('%Y-%m-%d')
            custom_date = False
        else:
            date_str, custom_date, text = parse_natural_date(text, dt)

        # Recompute the dispatch word AFTER date parsing has stripped any leading
        # date line (so "昨天open Assets:X CNY" still routes to the open handler)
        # and match the directive branches below on EXACT equality, so natural
        # language like "opened a beer 5 CNY" is not hijacked into a directive.
        dispatch_word = text.strip().split()[0].lower() if text.strip() else ""

        commit_message = 'Add entry by Telegram Bot\n\n'
        appendix = ""
        target_file_path = self.settings.FILE_PATH

        if text.startswith('/'):
            text = text[1:]
            command = text.split(' ', 1)[0]
            payload = text[len(command):].strip()
            log(f"Command: {command}, Payload: {payload}")
            if command == "tz":
                try:
                    self.timezone = pytz.timezone(payload)
                    self._save_pending()
                except pytz.UnknownTimeZoneError:
                    reply(f"Unknown timezone: {payload}")
                    return
                reply(f"Timezone set to {self.timezone}")
                reply(f"Current time: {datetime.now(self.timezone).strftime('%Y-%m-%d %H:%M:%S')}")
                return
            elif command == "update":
                parts = payload.split()
                if len(parts) != 4:
                    reply("Invalid update command format. Use: /update [account] [account for pad] [amount] [currency]")
                    return

                account = self.match_account(parts[0])
                if not account:
                    reply(f"No matching account found for suffix: {parts[0]}")
                    return
                if not account.startswith("Expenses") and not account.startswith("Income"):
                    commit_message += f"{account}\n"

                pad_account = self.match_account(parts[1])
                if not pad_account:
                    reply(f"No matching account found for suffix: {parts[1]}")
                    return

                amount = parts[2]
                try:
                    float(amount)
                except ValueError:
                    reply(f"Invalid amount: {amount}. Must be a valid number.")
                    return
                currency = parts[3]

                try:
                    today_date = datetime.strptime(date_str, '%Y-%m-%d')
                    tomorrow_date = (today_date + timedelta(days=1)).strftime('%Y-%m-%d')
                except ValueError:
                    tomorrow_date = (dt + timedelta(days=1)).strftime('%Y-%m-%d')

                pad_appendix = jinja2.get_template("pad.bean.j2").render(
                    date=date_str, account=account, pad_account=pad_account, datetime=datetime_str
                )
                balance_appendix = jinja2.get_template("balance.bean.j2").render(
                    date=tomorrow_date, account=account, amount=amount, currency=currency, datetime=datetime_str
                )
                appendix = pad_appendix + "\n\n" + balance_appendix
            elif command == "view":
                ok, err = self.github_trigger_workflow("monthly-report.yml", {})
                if ok:
                    reply("Sankey report is being generated.")
                else:
                    reply(f"Failed to trigger the report workflow: {err}")
                return
            elif command == "undo":
                self.handle_undo(chat_id)
                return
            elif command == "last":
                count = 5
                if payload:
                    try:
                        count = min(max(1, int(payload)), 50)
                    except ValueError:
                        reply("用法：/last [数量]，默认 5，最大 50")
                        return
                self.handle_last(chat_id, count)
                return
            elif command == "today":
                self.handle_today(chat_id)
                return
            else:
                reply(f"Unknown command: {command}")
                return

        elif dispatch_word == "open":
            log("/open command detected")
            matches = re.findall(r'.*?\s+([^\s]+)\s+([^\s]+)', text, re.IGNORECASE)
            if not matches or len(matches[0]) < 2:
                reply("Invalid open command format.")
                return
            account = matches[0][0]
            currency = matches[0][1]
            if not re.match(r'^[A-Z][a-zA-Z0-9]*(?::[A-Z][a-zA-Z0-9]*)+$', account):
                reply("Invalid account name. Must be colon-separated capitalized segments, e.g. Assets:Bank:Foo")
                return
            if not re.match(r'^[A-Z][A-Z0-9]{0,9}$', currency):
                reply("Invalid currency. Must be 1-10 uppercase alphanumeric characters starting with a letter, e.g. USD, CNY")
                return
            prefix = account.split(":")[0].lower()
            target_file_path = ACCOUNT_TYPE_MAP.get(prefix, self.settings.FILE_PATH)
            appendix = jinja2.get_template("open.bean.j2").render(
                date=date_str, account=account, currency=currency, datetime=datetime_str
            )

        elif dispatch_word == "close":
            log("/close command detected")
            matches = re.findall(r'.*?\s+([^\s]+)', text, re.IGNORECASE)
            if not matches:
                reply("Invalid close command format. Use: close [account]")
                return
            account_input = matches[0]
            account = self.match_account(account_input)
            if not account:
                reply(f"Account not found (no open record): {account_input}")
                return
            prefix = account.split(":")[0].lower()
            target_file_path = ACCOUNT_TYPE_MAP.get(prefix, self.settings.FILE_PATH)
            appendix = jinja2.get_template("close.bean.j2").render(
                date=date_str, account=account, datetime=datetime_str
            )

        elif dispatch_word == "balance":
            log("/balance command detected")
            matches = re.findall(r'.*?\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)', text, re.IGNORECASE)
            if not matches or len(matches[0]) < 3:
                reply("Invalid balance command format.")
                return
            account = self.match_account(matches[0][0])
            if not account:
                reply(f"No matching account found for suffix: {matches[0][0]}")
                return
            amount = matches[0][1]
            currency = matches[0][2]
            # balance assertions take effect on the *opening* of the stated date,
            # so the default is tomorrow (today + 1 day). Override by prefixing
            # the message with a YYYY-MM-DD date line.
            balance_date_str = date_str if custom_date else (dt + timedelta(days=1)).strftime('%Y-%m-%d')
            appendix = jinja2.get_template("balance.bean.j2").render(
                date=balance_date_str, account=account, amount=amount, currency=currency, datetime=datetime_str
            )

        elif dispatch_word == "pad":
            log("/pad command detected")
            matches = re.findall(r'.*?\s+([^\s]+)\s+([^\s]+)', text, re.IGNORECASE)
            if not matches or len(matches[0]) < 2:
                reply("Invalid pad command format.")
                return
            account = self.match_account(matches[0][0])
            if not account:
                reply(f"No matching account found for suffix: {matches[0][0]}")
                return
            pad_account = self.match_account(matches[0][1])
            if not pad_account:
                reply(f"No matching account found for suffix: {matches[0][1]}")
                return
            appendix = jinja2.get_template("pad.bean.j2").render(
                date=date_str, account=account, pad_account=pad_account, datetime=datetime_str
            )

        elif text.strip() and ("\n" not in text.strip()) and (not text.strip().startswith('/')):
            log("Single-line natural language detected, forwarding to LLM")
            if not self.llm_enabled:
                reply(self.llm_unavailable_message())
                return

            accounts = self.parse_accounts()
            if not accounts:
                reply("No accounts available. Please check GitHub account parsing first.")
                return

            route = self.route_intent(text, date_str)
            if route["intent"] == "query":
                try:
                    bql, rendered = self.answer_query(text, route["bql"], date_str)
                except Exception as e:
                    log(f"Query failed: {e}")
                    reply(f"查询未完成：{e}")
                    return
                # Send only the formatted result. The BQL stays in the console log
                # (Query BQL from LLM / BQL ok) — it's noise to the person asking.
                # format_query_result caps by code points; go through the UTF-16-aware
                # capper so an emoji-heavy table can't still overflow Telegram's cap on
                # this HTML path (which bypasses send_message's plain-text guard).
                block, _ = _capped_code_block(rendered, TELEGRAM_MESSAGE_LIMIT)
                self.send_message(chat_id, block, parse_mode="HTML")
                return

            # Between the two LLM calls that a text entry already makes (router above,
            # generator below), do free local lookups to enrich the draft prompt. Load the
            # ledger once here and hand it to both helpers so they don't each pay a separate
            # GitHub round trip for the same tree. Best-effort — never blocks.
            try:
                loaded_ledger = self.load_ledger()
            except Exception as e:
                log(f"Ledger load for payee context failed ({e}); generating without it.")
                loaded_ledger = None

            # If the router named a payee, feed the user's own past entries for that
            # merchant to the generator so it matches their account/narration conventions.
            examples = None
            payee_raw = route.get("payee")
            payee_hint = payee_raw.strip() if isinstance(payee_raw, str) else ""
            if payee_hint:
                try:
                    examples = self.examples_for_payee(payee_hint, loaded=loaded_ledger)
                    if examples:
                        log(f"Injecting past entries for payee {payee_hint!r} into the draft prompt.")
                except Exception as e:
                    log(f"Payee-history lookup failed ({e}); generating without examples.")

            # Also hand the LLM the user's most-used merchant names so it snaps a fuzzy
            # input onto an existing payee instead of coining a near-duplicate.
            payees = None
            try:
                payees = self.frequent_payees(loaded=loaded_ledger)
            except Exception as e:
                log(f"Frequent-payee lookup failed ({e}); generating without payee list.")

            try:
                appendix = self.call_openai_compatible(text, accounts, date_str, current_time="" if custom_date else time_str, examples=examples, payees=payees, loaded=loaded_ledger)
                appendix = self.insert_prompt_metadata(appendix, text)
                commit_message = self.add_non_pnl_accounts_to_commit_message(commit_message, appendix)

                pending_id = self.next_pending_id()
                with self._pending_lock:
                    self.pending_llm_entries[pending_id] = self._make_pending_entry(
                        chat_id, appendix, commit_message, message["message"]["text"], date_str
                    )

                log("LLM draft:\n" + appendix)
                self.send_draft_for_review(chat_id, "LLM draft (checked padding):", appendix, pending_id)
                return
            except Exception as e:
                log(f"LLM generation failed: {e}")
                error_text = str(e)
                if isinstance(e, LedgerValidationError) or _is_account_error(error_text):
                    reply(error_text)
                else:
                    reply(f"LLM generation failed: {e}")
                return

        else:
            log("Transaction detected")
            lines = text.splitlines()

            if len(lines) < 4:
                reply("Invalid transaction format. Please provide payee, narration and two postings.")
                return

            payee = lines.pop(0).strip()
            narration = lines.pop(0).strip()

            tag = None
            link = None
            while lines and lines[0].strip():
                if lines[0].startswith('#'):
                    tag = lines.pop(0)[1:].strip()
                elif lines[0].startswith('^'):
                    link = lines.pop(0)[1:].strip()
                else:
                    break

            if len(lines) < 2:
                reply("A transaction must have at least two postings.")
                return

            postings = []
            r_posting = r'([^\s]+)\s*(-?\d+\.?\d*)\s*([^\s]+)\s*(.*?)\s*$'
            for posting_line in lines:
                posting_line = posting_line.strip()
                if not posting_line:
                    continue

                posting_str, comment = posting_line.split(';', 1) if ';' in posting_line else (posting_line, "")
                pmatches = re.match(r_posting, posting_str)
                if not pmatches:
                    reply(f"Invalid posting format: {posting_str}")
                    return

                account = self.match_account(pmatches.group(1))
                if not account:
                    reply(f"No matching account found for suffix: {pmatches.group(1)}")
                    return
                if not account.startswith("Expenses") and not account.startswith("Income"):
                    commit_message += f"{account}\n"

                amount = pmatches.group(2)
                currency = pmatches.group(3)
                rest = pmatches.group(4) or ""

                # beancount commodities are 2-24 chars, start with an uppercase letter and end
                # with a letter or digit (a leading digit like "3NVD" parses as a number, a
                # single letter is rejected, and 24 is the max length). Mirror that exactly here
                # so the user gets a clear error instead of an opaque parser failure at commit.
                if not re.match(r"^[A-Z][A-Z0-9'._-]{0,22}[A-Z0-9]$", currency):
                    reply(f"货币符号 '{currency}' 无效：需以大写字母开头、字母或数字结尾，2-24 位，例如 USD、CNY、NVD3。")
                    return

                postings.append({
                    "account": account,
                    "amount": amount,
                    "currency": currency,
                    "rest": rest,
                    "comment": comment.strip()
                })

            if len(postings) == 2:
                a0, a1 = Decimal(postings[0]["amount"]), Decimal(postings[1]["amount"])
                c0, c1 = postings[0]["currency"], postings[1]["currency"]
                r0, r1 = postings[0]["rest"], postings[1]["rest"]

                if a0 * a1 >= 0:
                    reply("两条 posting 必须一正一负。")
                    return

                if c0 == c1:
                    if abs(a0 + a1) > BALANCE_TOLERANCE:
                        reply(f"同币种 {c0} 的两条 posting 金额不平衡：{a0} + {a1} != 0")
                        return
                else:
                    has_cost_or_price = any(('@' in r or '{' in r) for r in [r0, r1])
                    if not has_cost_or_price:
                        reply(f"不同币种 ({c0}/{c1}) 的交易需要标记成本 {{}} 或价格 @。")
                        return

            # The template drops payee/narration straight between quotes, so a literal " (or
            # \) would produce a malformed or silently-mangled directive. Escape both the same
            # way insert_prompt_metadata does (backslash first, then quote) and parse the
            # rendered result before committing, so a bad manual entry never poisons the ledger
            # and every downstream reader (/last, /today, /undo, NL→BQL).
            def _esc(s: str) -> str:
                return s.replace('\\', '\\\\').replace('"', '\\"')
            appendix = jinja2.get_template("transaction.bean.j2").render(
                date=date_str, payee=_esc(payee), narration=_esc(narration),
                postings=postings, tag=tag, link=link, datetime=datetime_str,
            )
            syntax_error = self.validate_beancount_syntax(appendix)
            if syntax_error:
                reply(f"生成的分录无法通过 beancount 校验：{syntax_error}")
                return

        ok, err = self.append_to_file(appendix, commit_message.strip(), target_file_path)
        if ok:
            self.send_message(
                chat_id,
                f"Created entry:\n{_code_block(appendix)}" if appendix else "Created entry",
                parse_mode="HTML",
            )
            log("Logged entry:\n" + appendix)
        else:
            reply(err)


def run():
    debug = len(sys.argv) > 1 and sys.argv[1] == "debug"
    bot = Bot(debug)
    signal.signal(signal.SIGTERM, lambda *_: bot.stop.set())
    if debug:
        log("Debug mode")
    try:
        bot.start()
    except KeyboardInterrupt:
        log("Exiting...")
    finally:
        bot.close()
