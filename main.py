import base64
import html
import json
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pprint import pprint

import pytz
import requests
from jinja2 import Environment, FileSystemLoader

from prompts import BEANCOUNT_SYSTEM_PROMPT, build_user_prompt

with open("config.json", "r") as f:
    config = json.load(f)


def get_int_config(name: str, default: int, minimum: int = 1) -> int:
    value = config.get(name, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        log(f"Invalid config `{name}`: {value}, fallback to {default}")
        return default
    if parsed < minimum:
        log(f"Config `{name}` is too small: {parsed}, fallback to {default}")
        return default
    return parsed

GITHUB_URL_BASE = "https://api.github.com"
GITHUB_TOKEN = config["GITHUB_TOKEN"]
REPO_OWNER = config["REPO_OWNER"]
REPO_NAME = config["REPO_NAME"]
BRANCH_NAME = config["BRANCH_NAME"]
FILE_PATH = config["FILE_PATH"]
CHAT_ID = config.get("CHAT_ID", None)


def _parse_llm_backends() -> list[dict]:
    raw = config.get("LLM_BACKENDS")
    if isinstance(raw, list):
        backends = []
        for b in raw:
            url = b.get("LLM_API_BASE_URL", "").rstrip("/")
            key = b.get("LLM_API_KEY", "")
            model = b.get("LLM_MODEL", "")
            if url and key and model:
                backends.append({"base_url": url, "api_key": key, "model": model})
        return backends
    # Backward compat: single-backend keys
    url = config.get("LLM_API_BASE_URL", "").rstrip("/")
    key = config.get("LLM_API_KEY", "")
    model = config.get("LLM_MODEL", "")
    if url and key and model:
        return [{"base_url": url, "api_key": key, "model": model}]
    return []


LLM_BACKENDS = _parse_llm_backends()
LLM_ENABLED = len(LLM_BACKENDS) > 0
LLM_MISSING_CONFIG_KEYS = [] if LLM_ENABLED else ["LLM_BACKENDS"]

jinja2 = Environment(loader=FileSystemLoader(searchpath="./templates"))


class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


print_lock = threading.Lock()


def log(message):
    with print_lock:
        print(f"\r \r{bcolors.OKGREEN}[{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}]{bcolors.ENDC}", end="")
        if isinstance(message, str):
            print(" " + message)
        else:
            print()
            pprint(message)


class rotating_loading:
    def __init__(self, stop_event: threading.Event):
        self.stop_event = stop_event

    def start(self):
        symbols = ['/', '-', '\\', '|']
        duration = 0.2
        i = 0
        while not self.stop_event.wait(duration):
            with print_lock:
                print('\r' + symbols[i % len(symbols)], end='', flush=True)
            i += 1
        with print_lock:
            print('\r \r', end='', flush=True)


ACCOUNTS_CACHE_TTL = get_int_config("ACCOUNTS_CACHE_TTL", 300)
DRAFT_TTL_SECONDS = get_int_config("DRAFT_TTL_SECONDS", 30)

GITHUB_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.object",
    "X-GitHub-Api-Version": "2022-11-28"
}


class Bot:
    def __init__(self, debug: bool = False):
        self.update_id = 0
        self.debug = debug
        self.stop = threading.Event()
        self.timezone = pytz.timezone(config["TIMEZONE"])
        self.api_base = "https://api.telegram.org/bot{}".format(config["TELEGRAM_BOT_TOKEN"])
        self.pending_llm_entries = {}
        self.pending_llm_id = 0
        self.pending_decline_reasons = {}
        self._accounts_cache = {"accounts": None, "ts": 0}
        self.llm_enabled = LLM_ENABLED

        if not self.llm_enabled:
            log(
                "LLM disabled: missing config "
                + ", ".join(LLM_MISSING_CONFIG_KEYS)
                + ". Natural language input will not be processed."
            )

    def llm_unavailable_message(self) -> str:
        if self.llm_enabled:
            return ""
        missing = ", ".join(LLM_MISSING_CONFIG_KEYS)
        return (
            "LLM is not fully configured, unable to process natural language. "
            f"Missing: {missing}."
        )

    def has_account_or_payment_hint(self, user_input: str, accounts: list[str]) -> bool:
        text = user_input.strip().lower()
        if not text:
            return False

        if "微信" in user_input or "wechat" in text:
            return True

        # Explicit beancount-like account path in free text.
        if re.search(r'\b[A-Z][A-Za-z0-9_-]*:[A-Za-z0-9_:-]+\b', user_input):
            return True

        # Match input against known account segments/suffixes.
        for account in accounts:
            account_lower = account.lower()
            orig_segments = [s for s in account.split(":") if len(s) >= 3]
            segments = [s.lower() for s in orig_segments]
            candidates = {account_lower}
            candidates.update(segments)
            # Also add camelCase-split variants (e.g. "GlobalMoney" → "global money")
            for orig_seg in orig_segments:
                spaced = re.sub(r'(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])', ' ', orig_seg).lower()
                if spaced != orig_seg.lower():
                    candidates.add(spaced)
            if len(segments) >= 2:
                candidates.add(segments[-2] + ":" + segments[-1])
            if any(candidate and candidate in text for candidate in candidates):
                return True

        return False

    def missing_account_hint_message(self) -> str:
        return (
            "无法判断付款账户，请补充账户线索后重试。"
            "例如：账户后缀（HSBC:Current）或完整账户名（Assets:HSBC:Current）。"
        )

    def parse_accounts(self):
        now = time.time()
        if self._accounts_cache["accounts"] is not None and now - self._accounts_cache["ts"] < ACCOUNTS_CACHE_TTL:
            return self._accounts_cache["accounts"]

        list_headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        url = f"{GITHUB_URL_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/accounts?ref={BRANCH_NAME}"
        r = requests.get(url, headers=list_headers)
        if r.status_code != 200:
            log(f"Error fetching accounts: {r.status_code}")
            log(r.text)
            return []
        accounts = []
        for item in r.json():
            if not item["name"].endswith(".bean"):
                continue
            file_r = requests.get(item["url"], headers=list_headers)
            if file_r.status_code != 200:
                continue
            content = base64.b64decode(file_r.json()["content"]).decode("utf-8")
            for m in re.findall(r'\d{4}-\d{2}-\d{2} open (.*)', content):
                accounts.append(m.strip().split(" ", 1)[0])
            for m in re.findall(r'\d{4}-\d{2}-\d{2} close (.*)', content):
                closed = m.strip().split(" ", 1)[0]
                if closed in accounts:
                    accounts.remove(closed)

        accounts.sort()
        self._accounts_cache["accounts"] = accounts
        self._accounts_cache["ts"] = now
        return accounts

    def match_account(self, account_suffix: str) -> str | None:
        accounts = self.parse_accounts()
        suffix_lower = account_suffix.lower()
        matches = [a for a in accounts if a.lower().endswith(suffix_lower)]
        if not matches:
            log(f"No matching account for suffix: {account_suffix}")
            log(f"Available accounts: {accounts}")
        return matches[0] if matches else None

    def prefer_current_account(self, account: str, accounts: list[str]) -> str:
        accounts_by_lower = {a.lower(): a for a in accounts}

        if account.lower() in accounts_by_lower:
            return accounts_by_lower[account.lower()]

        # Don't add :Current suffix for Liabilities accounts (credit cards)
        if not account.lower().startswith("liabilities:"):
            if ":current" not in account.lower():
                current_candidate = f"{account}:Current"
                if current_candidate.lower() in accounts_by_lower:
                    return accounts_by_lower[current_candidate.lower()]

        return account

    def strip_code_fence(self, text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            lines = stripped.splitlines()
            if len(lines) >= 3:
                return "\n".join(lines[1:-1]).strip()
        return stripped

    def normalize_and_validate_llm_entry(self, entry_text: str, accounts: list[str]) -> str:
        text = self.strip_code_fence(entry_text)
        raw_lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        if len(raw_lines) < 3:
            raise ValueError("LLM output is too short. Expected a transaction header and at least two postings.")

        header = raw_lines[0].strip()
        metadata_lines = []
        postings = []

        posting_re = re.compile(r'^\s*(\S+)\s+(-?\d+(?:\.\d+)?)\s+(\S+)(?:\s+(.*))?$')

        for line in raw_lines[1:]:
            pm = posting_re.match(line)
            if pm:
                account = self.prefer_current_account(pm.group(1), accounts)
                amount = pm.group(2)
                currency = pm.group(3)
                rest = (pm.group(4) or "").strip()
                postings.append({
                    "account": account,
                    "amount": amount,
                    "currency": currency,
                    "rest": rest,
                })
                continue

            # Keep metadata/comment-like lines and normalize indentation.
            metadata_lines.append(f"  {line.strip()}")

        if len(postings) < 2:
            raise ValueError("LLM output must contain at least two postings.")

        currencies = set(p["currency"] for p in postings)
        if len(currencies) == 1:
            total = sum(float(p["amount"]) for p in postings)
            if abs(total) > 0.0001:
                raise ValueError(f"LLM output invalid: postings do not balance (sum = {total:.4f}).")

        if len(postings) == 2:
            a0 = float(postings[0]["amount"])
            a1 = float(postings[1]["amount"])
            c0 = postings[0]["currency"]
            c1 = postings[1]["currency"]
            r0 = postings[0]["rest"]
            r1 = postings[1]["rest"]

            if a0 * a1 >= 0:
                raise ValueError("LLM output invalid: two postings must be one positive and one negative.")

            if c0 == c1 and (a0 + a1) != 0:
                raise ValueError(f"LLM output invalid: same-currency postings are unbalanced ({a0} + {a1} != 0).")

            if c0 != c1:
                has_cost_or_price = any(('@' in r or '{' in r) for r in [r0, r1])
                if not has_cost_or_price:
                    # Auto-insert FX price annotation when LLM misses @/@@ on cross-currency postings.
                    abs0, abs1 = abs(a0), abs(a1)
                    if abs0 == 0 and abs1 == 0:
                        raise ValueError("LLM output invalid: zero amounts in cross-currency postings.")

                    # Put cost mark on the side that yields the larger numerical FX rate.
                    if abs0 <= abs1 and abs0 != 0:
                        rate = abs1 / abs0
                        rate_str = f"{rate:.8f}".rstrip('0').rstrip('.')
                        postings[0]["rest"] = (postings[0]["rest"] + f" @ {rate_str} {c1}").strip()
                    elif abs1 != 0:
                        rate = abs0 / abs1
                        rate_str = f"{rate:.8f}".rstrip('0').rstrip('.')
                        postings[1]["rest"] = (postings[1]["rest"] + f" @ {rate_str} {c0}").strip()
                    else:
                        total_str = f"{abs1:.8f}".rstrip('0').rstrip('.')
                        postings[0]["rest"] = (postings[0]["rest"] + f" @@ {total_str} {c1}").strip()

        account_width = max(len(p["account"]) for p in postings) + 2
        amount_width = max(len(str(p["amount"])) for p in postings) + 2
        currency_width = max(len(p["currency"]) for p in postings) + 2

        out = [header]
        out.extend(metadata_lines)
        for p in postings:
            line = (
                "  "
                + p["account"].ljust(account_width)
                + " "
                + p["amount"].rjust(amount_width)
                + " "
                + p["currency"].ljust(currency_width)
            )
            if p["rest"]:
                line += f" {p['rest']}"
            out.append(line.rstrip())

        return "\n".join(out)

    def extract_accounts_from_entry(self, entry_text: str) -> list[str]:
        accounts = []
        posting_line_re = re.compile(r'^\s+(\S+)\s+')
        for line in entry_text.splitlines():
            m = posting_line_re.match(line)
            if m:
                accounts.append(m.group(1))
        return accounts

    def ensure_datetime_metadata(self, entry_text: str, datetime_str: str) -> str:
        lines = entry_text.splitlines()
        if not lines:
            return entry_text

        has_datetime = any(re.match(r'^\s*datetime\s*:\s*".*"\s*$', line) for line in lines[1:])
        if has_datetime:
            return entry_text

        return "\n".join([lines[0], f'  datetime: "{datetime_str}"'] + lines[1:])

    def call_openai_compatible(
        self,
        user_input: str,
        accounts: list[str],
        txn_date: str,
        previous_draft: str | None = None,
        decline_reason: str | None = None,
    ) -> str:
        if not self.llm_enabled:
            raise ValueError(self.llm_unavailable_message())

        system_prompt = BEANCOUNT_SYSTEM_PROMPT
        user_prompt = build_user_prompt(txn_date, accounts, user_input, previous_draft, decline_reason)
        payload = {
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        last_error: Exception | None = None
        for backend in LLM_BACKENDS:
            try:
                url = f"{backend['base_url']}/chat/completions"
                headers = {
                    "Authorization": f"Bearer {backend['api_key']}",
                    "Content-Type": "application/json",
                }
                response = requests.post(url, headers=headers, json={**payload, "model": backend["model"]}, timeout=60)
                response.raise_for_status()
                data = response.json()
                raw_text = data["choices"][0]["message"]["content"].strip()

                if raw_text.upper().startswith("NEED_ACCOUNT:"):
                    guidance = raw_text.split(":", 1)[1].strip() if ":" in raw_text else ""
                    raise ValueError(guidance or "请在输入中提供至少一个账户名（或账户后缀），我才能生成分录。")

                try:
                    return self.normalize_and_validate_llm_entry(raw_text, accounts)
                except Exception as e:
                    raise ValueError(f"{e}\nInvalid LLM output:\n{raw_text}") from e

            except ValueError:
                raise
            except Exception as e:
                log(f"LLM backend '{backend['model']}' failed: {e}, trying next...")
                last_error = e

        raise ValueError(f"All LLM backends failed. Last error: {last_error}")

    def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        data = {"chat_id": chat_id, "text": text}
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup is not None:
            data["reply_markup"] = reply_markup
        response = requests.post(self.api_base + "/sendMessage", json=data)
        if response.status_code != 200:
            log(f"Error sending message: {response.status_code}")
            log(response.text)
        return response.json()

    def answer_callback_query(self, callback_query_id, text=None):
        data = {"callback_query_id": callback_query_id}
        if text:
            data["text"] = text
        requests.post(self.api_base + "/answerCallbackQuery", json=data)

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        data = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": reply_markup or {"inline_keyboard": []},
        }
        requests.post(self.api_base + "/editMessageReplyMarkup", json=data)

    def next_pending_id(self) -> str:
        self.pending_llm_id += 1
        return str(self.pending_llm_id)

    def remove_decline_reason_bindings(self, pending_id: str):
        for reason_chat_id, reason_pending_id in list(self.pending_decline_reasons.items()):
            if reason_pending_id == pending_id:
                self.pending_decline_reasons.pop(reason_chat_id, None)

    def add_non_pnl_accounts_to_commit_message(self, commit_message: str, entry_text: str) -> str:
        for account in self.extract_accounts_from_entry(entry_text):
            if not account.startswith(("Expenses", "Income")):
                commit_message += f"{account}\n"
        return commit_message

    def is_pending_expired(self, pending: dict) -> bool:
        created_at = pending.get("created_at", 0)
        return (time.time() - created_at) > DRAFT_TTL_SECONDS

    def cleanup_expired_drafts(self):
        expired_ids = [
            pending_id
            for pending_id, pending in self.pending_llm_entries.items()
            if self.is_pending_expired(pending)
        ]

        for pending_id in expired_ids:
            pending = self.pending_llm_entries.pop(pending_id, None)
            if not pending:
                continue

            chat_id = pending.get("chat_id")
            if chat_id is not None:
                self.send_message(chat_id, f"Draft expired after {DRAFT_TTL_SECONDS} seconds and was discarded.")

            self.remove_decline_reason_bindings(pending_id)

    def build_review_buttons(self, pending_id: str):
        return {
            "inline_keyboard": [[
                {"text": "✅", "callback_data": f"approve:{pending_id}"},
                {"text": "🔧", "callback_data": f"decline_reason:{pending_id}"},
                {"text": "❌", "callback_data": f"discard:{pending_id}"},
            ]]
        }

    def run_recheck(self, chat_id: int, pending_id: str, decline_reason: str | None = None):
        pending = self.pending_llm_entries.get(pending_id)
        if not pending:
            self.send_message(chat_id, "This request is expired or already handled")
            return

        if not self.llm_enabled:
            self.send_message(chat_id, self.llm_unavailable_message())
            return

        if decline_reason:
            log(f"Running LLM recheck with reason: {decline_reason}")

        accounts = self.parse_accounts()
        if not accounts:
            self.pending_llm_entries.pop(pending_id, None)
            self.send_message(chat_id, "No accounts available. Please check GitHub account parsing first.")
            return

        hint_text = pending["user_input"] + (f"\n{decline_reason}" if decline_reason else "")
        if not self.has_account_or_payment_hint(hint_text, accounts):
            self.send_message(chat_id, self.missing_account_hint_message())
            return

        try:
            new_appendix = self.call_openai_compatible(
                pending["user_input"],
                accounts,
                pending["date_str"],
                previous_draft=pending["appendix"],
                decline_reason=decline_reason,
            )
            new_commit_message = self.add_non_pnl_accounts_to_commit_message(
                'Add entry by Telegram Bot\n\n', new_appendix
            )

            new_pending_id = self.next_pending_id()
            self.pending_llm_entries[new_pending_id] = {
                "chat_id": chat_id,
                "appendix": new_appendix,
                "commit_message": new_commit_message,
                "created_at": time.time(),
                "user_input": pending["user_input"],
                "date_str": pending["date_str"],
            }
            self.pending_llm_entries.pop(pending_id, None)

            self.send_message(
                chat_id,
                "LLM rechecked draft:\n"
                f"<pre><code>{html.escape(new_appendix)}</code></pre>\n"
                "Use ✅ to save, 🔧 to provide feedback, or ❌ to discard.",
                reply_markup=self.build_review_buttons(new_pending_id),
                parse_mode="HTML",
            )
        except Exception as e:
            self.pending_llm_entries.pop(pending_id, None)
            log(f"LLM recheck failed: {e}")
            error_text = str(e)
            if error_text and ("账户" in error_text or "account" in error_text.lower()):
                self.send_message(chat_id, error_text)
            else:
                self.send_message(chat_id, f"LLM recheck failed: {e}")

    def handle_callback_query(self, update):
        callback = update["callback_query"]
        callback_id = callback["id"]
        data = callback.get("data", "")
        message = callback.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")

        try:
            action, pending_id = data.split(":", 1)
        except ValueError:
            self.answer_callback_query(callback_id, "Unknown action")
            return

        pending = self.pending_llm_entries.get(pending_id)
        if not pending:
            self.answer_callback_query(callback_id, "This request is expired or already handled")
            return

        if self.is_pending_expired(pending):
            self.pending_llm_entries.pop(pending_id, None)
            self.remove_decline_reason_bindings(pending_id)
            self.answer_callback_query(callback_id, "Expired")
            self.send_message(chat_id, f"Draft expired after {DRAFT_TTL_SECONDS} seconds and was discarded.")
            return

        if chat_id != pending["chat_id"]:
            self.answer_callback_query(callback_id, "Not allowed")
            return

        self.edit_message_reply_markup(chat_id, message_id)

        if action == "decline_reason":
            if not self.llm_enabled:
                self.answer_callback_query(callback_id, "LLM unavailable")
                self.send_message(chat_id, self.llm_unavailable_message())
                return

            self.answer_callback_query(callback_id, "Please send reason")
            self.pending_decline_reasons[chat_id] = pending_id
            self.send_message(chat_id, "Please send your decline reason as plain text. I will send it to LLM for recheck.")
            return

        if action == "discard":
            self.pending_llm_entries.pop(pending_id, None)
            self.answer_callback_query(callback_id, "Discarded")
            self.send_message(chat_id, "Discarded. Entry was not saved.")
            return

        if action != "approve":
            self.answer_callback_query(callback_id, "Unknown action")
            return

        f = self.github_download_file()
        if not f:
            self.answer_callback_query(callback_id, "Failed")
            self.send_message(chat_id, "Failed to download from GitHub.")
            return

        appendix = pending["appendix"]
        approve_datetime_str = datetime.now(self.timezone).strftime('%Y-%m-%d %H:%M:%S')
        appendix = self.ensure_datetime_metadata(appendix, approve_datetime_str)
        commit_message = pending["commit_message"]
        ok = self.github_upload_file(f["content"] + '\n' + appendix + '\n', f["sha"], commit_message.strip())
        self.pending_llm_entries.pop(pending_id, None)

        if ok:
            self.answer_callback_query(callback_id, "Approved")
            self.send_message(chat_id, f"Created entry:\n<pre><code>{html.escape(appendix)}</code></pre>", parse_mode="HTML")
            log("Logged entry:\n" + appendix)
        else:
            self.answer_callback_query(callback_id, "Failed")
            self.send_message(chat_id, "Failed to upload to GitHub.")

    def github_download_file(self, file_path: str = FILE_PATH) -> dict | None:
        url = f"{GITHUB_URL_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{file_path}?ref={BRANCH_NAME}"
        r = requests.get(url=url, headers=GITHUB_HEADERS)
        if r.status_code == 200:
            return {
                "content": base64.b64decode(r.json()["content"]).decode("utf-8"),
                "sha": r.json()["sha"]
            }
        elif r.status_code == 404:
            log("File not found.")
            return {"content": "", "sha": ""}
        else:
            log(f"Error: {r.status_code}")
            return None

    def github_upload_file(self, content: str, sha: str, commit_message: str, file_path: str = FILE_PATH) -> bool:
        url = f"{GITHUB_URL_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{file_path}"
        data = {
            "message": commit_message,
            "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
            "branch": BRANCH_NAME,
        }
        if sha:
            data["sha"] = sha
        r = requests.put(url=url, headers=GITHUB_HEADERS, json=data)
        if r.status_code in [200, 201]:
            return True
        else:
            log(f"Error uploading file: {r.status_code}")
            log(r.text)
            return False

    def handle_message(self, message):
        text = message["message"]["text"]
        chat_id = message["message"]["chat"]["id"]

        def reply(text: str):
            self.send_message(chat_id, text)

        if CHAT_ID and str(chat_id) != CHAT_ID:
            log(f"Ignoring message from chat_id {chat_id}, only responding to {CHAT_ID}.")
            reply("How dare you?")
            return

        dt = datetime.now(self.timezone)
        date_str = dt.strftime('%Y-%m-%d')
        datetime_str = dt.strftime('%Y-%m-%d %H:%M:%S')

        if re.match(r'^\d{4}-\d{2}-\d{2}', text.strip()):
            log("Custom date detected")
            date_str = text.strip().splitlines()[0].strip()
            text = '\n'.join(text.strip().splitlines()[1:]).strip()

        pending_reason_id = self.pending_decline_reasons.get(chat_id)
        if pending_reason_id is not None:
            reason_text = text.strip()
            if not reason_text or reason_text.startswith('/'):
                reply("Please send a non-command reason text, or tap discard.")
                return

            self.pending_decline_reasons.pop(chat_id, None)
            log(f"Decline reason received: {reason_text}")
            self.run_recheck(chat_id, pending_reason_id, decline_reason=reason_text)
            return

        commit_message = 'Add entry by Telegram Bot\n\n'
        appendix = ""
        target_file_path = FILE_PATH

        if text.startswith('/'):
            text = text[1:]
            command = text.split(' ', 1)[0]
            payload = text[len(command):].strip()
            log(f"Command: {command}, Payload: {payload}")
            if command == "tz":
                if payload == 'London':
                    self.timezone = pytz.timezone("Europe/London")
                elif payload == 'Beijing':
                    self.timezone = pytz.timezone("Asia/Shanghai")
                else:
                    try:
                        self.timezone = pytz.timezone(payload)
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
            else:
                reply(f"Unknown command: {command}")
                return

        elif text.lower().startswith("open"):
            log("/open command detected")
            matches = re.findall(r'.*?\s+([^\s]+)\s+([^\s]+)', text, re.IGNORECASE)
            if not matches or len(matches[0]) < 2:
                reply("Invalid open command format.")
                return
            account = matches[0][0]
            currency = matches[0][1]
            account_type_map = {
                "assets": "accounts/assets.bean",
                "liabilities": "accounts/liabilities.bean",
                "equity": "accounts/equity.bean",
                "income": "accounts/income.bean",
                "expenses": "accounts/expenses.bean",
            }
            prefix = account.split(":")[0].lower()
            target_file_path = account_type_map.get(prefix, FILE_PATH)
            appendix = jinja2.get_template("open.bean.j2").render(
                date=date_str, account=account, currency=currency, datetime=datetime_str
            )

        elif text.lower().startswith("balance"):
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
            appendix = jinja2.get_template("balance.bean.j2").render(
                date=date_str, account=account, amount=amount, currency=currency, datetime=datetime_str
            )

        elif text.lower().startswith("pad"):
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
            if not self.has_account_or_payment_hint(text, accounts):
                reply(self.missing_account_hint_message())
                return
            try:
                appendix = self.call_openai_compatible(text, accounts, date_str)
                commit_message = self.add_non_pnl_accounts_to_commit_message(commit_message, appendix)

                pending_id = self.next_pending_id()
                self.pending_llm_entries[pending_id] = {
                    "chat_id": chat_id,
                    "appendix": appendix,
                    "commit_message": commit_message,
                    "created_at": time.time(),
                    "user_input": text,
                    "date_str": date_str,
                }

                self.send_message(
                    chat_id,
                    "LLM draft (checked padding):\n"
                    f"<pre><code>{html.escape(appendix)}</code></pre>\n"
                    "Use ✅ to save, 🔧 to provide feedback, or ❌ to discard.",
                    reply_markup=self.build_review_buttons(pending_id),
                    parse_mode="HTML",
                )
                return
            except Exception as e:
                log(f"LLM generation failed: {e}")
                error_text = str(e)
                if error_text and ("账户" in error_text or "account" in error_text.lower()):
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

                if not re.match(r'^[A-Z0-9][A-Z0-9\'._-]*$', currency) or not re.search(r'[A-Z]', currency):
                    reply(f"货币符号 '{currency}' 无效：必须全部大写，可包含数字，例如 USD、CNY、3NVD。")
                    return

                postings.append({
                    "account": account,
                    "amount": amount,
                    "currency": currency,
                    "rest": rest,
                    "comment": comment.strip()
                })

            if len(postings) == 2:
                a0, a1 = float(postings[0]["amount"]), float(postings[1]["amount"])
                c0, c1 = postings[0]["currency"], postings[1]["currency"]
                r0, r1 = postings[0]["rest"], postings[1]["rest"]

                if a0 * a1 >= 0:
                    reply("两条 posting 必须一正一负。")
                    return

                if c0 == c1:
                    if a0 + a1 != 0:
                        reply(f"同币种 {c0} 的两条 posting 金额不平衡：{a0} + {a1} != 0")
                        return
                else:
                    has_cost_or_price = any(('@' in r or '{' in r) for r in [r0, r1])
                    if not has_cost_or_price:
                        reply(f"不同币种 ({c0}/{c1}) 的交易需要标记成本 {{}} 或价格 @。")
                        return

            appendix = jinja2.get_template("transaction.bean.j2").render(
                date=date_str, payee=payee, narration=narration,
                postings=postings, tag=tag, link=link, datetime=datetime_str,
            )

        f = self.github_download_file(target_file_path)
        if not f:
            reply("Failed to download from GitHub.")
            return
        if self.github_upload_file(f["content"] + '\n' + appendix + '\n', f["sha"], commit_message.strip(), target_file_path):
            self.send_message(
                chat_id,
                f"Created entry:\n<pre><code>{html.escape(appendix)}</code></pre>" if appendix else "Created entry",
                parse_mode="HTML",
            )
            log("Logged entry:\n" + appendix)
        else:
            reply("Failed to upload to GitHub.")

    def get_updates(self):
        params = {"offset": self.update_id + 1, "timeout": 30}
        try:
            loading_stop_event = threading.Event()
            loading = threading.Thread(target=rotating_loading(loading_stop_event).start)
            loading.start()
            response = requests.get(self.api_base + "/getUpdates", data=params, timeout=params["timeout"] + 1)
            loading_stop_event.set()

            if response.status_code != 200:
                log(f"Error: {response.status_code}")
                return {"result": []}
        except KeyboardInterrupt:
            log("Got KeyboardInterrupt in Bot thread.")
            loading_stop_event.set()
            self.stop.set()
            exit(0)
        except Exception as e:
            log(e)
            log("Timeout or Connection Error")
            return {"result": []}
        return response.json()

    def process_updates(self):
        self.cleanup_expired_drafts()
        updates = self.get_updates()
        edited_messages = [x for x in updates["result"] if "edited_message" in x]
        callback_queries = [x for x in updates["result"] if "callback_query" in x]
        messages = [x for x in updates["result"] if "message" in x]

        if self.debug:
            pprint(updates)

        for message in edited_messages:
            self.update_id = message["update_id"]
            log(message)

        for callback in callback_queries:
            self.update_id = callback["update_id"]
            hd = threading.Thread(target=self.handle_callback_query, args=(callback,))
            hd.start()

        for message in messages:
            self.update_id = message["update_id"]
            chat = message["message"]["chat"]
            chat_id = chat["id"]
            first_name = chat.get("first_name", "")
            last_name = chat.get("last_name", "")
            username = chat.get("username", "")
            text = message["message"].get("text")

            fmt = f"{bcolors.OKBLUE}[{chat_id}]{bcolors.ENDC} {first_name} {last_name} (@{username}):"
            if text:
                hd = threading.Thread(target=self.handle_message, args=(message,))
                hd.start()
                if len(text.splitlines()) > 1:
                    log(f"{fmt} \n{text}")
                else:
                    log(f"{fmt} {text}")
            else:
                obj = {k: v for k, v in message['message'].items() if k not in ['chat', 'date', 'from', 'message_id']}
                log(f"{fmt} {obj}")

    def start(self):
        while not self.stop.is_set():
            self.process_updates()


if __name__ == "__main__":
    debug = len(sys.argv) > 1 and sys.argv[1] == "debug"
    bot = Bot(debug)
    if debug:
        log("Debug mode")
    try:
        bot.start()
    except KeyboardInterrupt:
        log("Exiting...")
