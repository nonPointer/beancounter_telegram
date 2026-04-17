import base64
import html
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pprint import pformat

import parsedatetime as pdt
import pytz
from dateutil.parser import parse as dateutil_parse
import requests
from beancount.parser import parser as beancount_parser
from jinja2 import Environment, FileSystemLoader

from prompts import (
    BEANCOUNT_SYSTEM_PROMPT, build_user_prompt,
    INVEST_ORDER_SYSTEM_PROMPT, build_invest_order_prompt,
    EXPENSE_SCREENSHOT_SYSTEM_PROMPT, build_expense_screenshot_prompt,
)

MAX_BEANCOUNT_RETRIES = 3

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
            vision_model = b.get("LLM_VISION_MODEL", "")
            if url and key and model:
                entry = {"base_url": url, "api_key": key, "model": model}
                if vision_model:
                    entry["vision_model"] = vision_model
                backends.append(entry)
        return backends
    return []


LLM_BACKENDS = _parse_llm_backends()
LLM_ENABLED = len(LLM_BACKENDS) > 0
LLM_MISSING_CONFIG_KEYS = [] if LLM_ENABLED else ["LLM_BACKENDS"]

jinja2 = Environment(loader=FileSystemLoader(searchpath="./templates"))


_C_GREEN = '\033[92m'
_C_BLUE = '\033[94m'
_C_RESET = '\033[0m'


print_lock = threading.Lock()


def log(message):
    with print_lock:
        print(f"\r \r{_C_GREEN}[{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}]{_C_RESET}", end="")
        if isinstance(message, str):
            print(" " + message)
        else:
            print(" " + pformat(message))


_FAKE_YEAR = 9999
_pdt_consts = pdt.Constants(usePyICU=False)
_pdt_consts.DOWParseStyle = -1  # "Monday" → today or the *last* Monday
_pdt_calendar = pdt.Calendar(_pdt_consts, version=pdt.VERSION_CONTEXT_STYLE)


def parse_natural_date(text: str, now: datetime) -> tuple[str, bool, str]:
    """Parse a natural-language date from the first line of *text*.

    Uses a two-layer fallback inspired by jrnl:
      1. dateutil  — structured dates (``2024-03-15``, ``March 15``, ``3/15``)
      2. parsedatetime — relative / fuzzy dates (``yesterday``, ``last friday``,
         ``3 days ago``)

    Returns ``(date_str, custom_date, remaining_text)`` where *date_str* is
    ``YYYY-MM-DD``, *custom_date* is ``True`` when a date was detected, and
    *remaining_text* is the input with the date line stripped.
    """
    if not text or not text.strip():
        return now.strftime('%Y-%m-%d'), False, text

    lines = text.strip().splitlines()
    first_line = lines[0].strip()

    if not first_line:
        return now.strftime('%Y-%m-%d'), False, text

    # --- Layer 0: Chinese date keywords (prefix matching) ---
    _chinese_date_map = {
        '大前天': timedelta(days=-3),
        '前天': timedelta(days=-2),
        '前晚': timedelta(days=-2),
        '昨天': timedelta(days=-1),
        '昨晚': timedelta(days=-1),
        '昨早': timedelta(days=-1),
        '今天': timedelta(days=0),
        '今晚': timedelta(days=0),
        '今早': timedelta(days=0),
        '明天': timedelta(days=1),
        '明早': timedelta(days=1),
        '明晚': timedelta(days=1),
        '后天': timedelta(days=2),
    }
    _cn_day_names = {'一': 0, '二': 1, '三': 2, '四': 3,
                     '五': 4, '六': 5, '日': 6, '天': 6}
    for _kw, _delta in _chinese_date_map.items():
        if first_line.startswith(_kw):
            d = now + _delta
            date_str = d.strftime('%Y-%m-%d')
            rest_of_first = first_line[len(_kw):].strip()
            remaining_lines = [rest_of_first] if rest_of_first else []
            remaining_lines.extend(lines[1:])
            remaining = '\n'.join(remaining_lines).strip()
            log(f"Custom date detected (Chinese keyword): {date_str}")
            return date_str, True, remaining
    _chinese_weekday_re = re.match(
        r'^(上+|下+)(?:周|星期|礼拜)([一二三四五六日天])\s*(.*)',
        first_line, re.DOTALL)
    if _chinese_weekday_re:
        prefix, day_char, rest_of_first = _chinese_weekday_re.groups()
        target_wd = _cn_day_names[day_char]
        current_wd = now.weekday()
        if prefix[0] == '上':
            weeks_back = len(prefix)
            diff = current_wd - target_wd
            if diff <= 0:
                diff += 7
            delta = diff + 7 * (weeks_back - 1)
            d = now - timedelta(days=delta)
        else:  # 下
            weeks_fwd = len(prefix)
            diff = target_wd - current_wd
            if diff <= 0:
                diff += 7
            delta = diff + 7 * (weeks_fwd - 1)
            d = now + timedelta(days=delta)
        date_str = d.strftime('%Y-%m-%d')
        rest_of_first = rest_of_first.strip()
        remaining_lines = [rest_of_first] if rest_of_first else []
        remaining_lines.extend(lines[1:])
        remaining = '\n'.join(remaining_lines).strip()
        log(f"Custom date detected (Chinese weekday): {date_str}")
        return date_str, True, remaining
    # Check for "N天前" / "N天后" pattern
    _chinese_ago_re = re.match(r'^(\d+)\s*天前\s*(.*)', first_line, re.DOTALL)
    _chinese_later_re = re.match(r'^(\d+)\s*天后\s*(.*)', first_line, re.DOTALL)
    if _chinese_ago_re:
        days = int(_chinese_ago_re.group(1))
        rest_of_first = _chinese_ago_re.group(2).strip()
        d = now - timedelta(days=days)
        date_str = d.strftime('%Y-%m-%d')
        remaining_lines = [rest_of_first] if rest_of_first else []
        remaining_lines.extend(lines[1:])
        remaining = '\n'.join(remaining_lines).strip()
        log(f"Custom date detected (Chinese N天前): {date_str}")
        return date_str, True, remaining
    if _chinese_later_re:
        days = int(_chinese_later_re.group(1))
        rest_of_first = _chinese_later_re.group(2).strip()
        d = now + timedelta(days=days)
        date_str = d.strftime('%Y-%m-%d')
        remaining_lines = [rest_of_first] if rest_of_first else []
        remaining_lines.extend(lines[1:])
        remaining = '\n'.join(remaining_lines).strip()
        log(f"Custom date detected (Chinese N天后): {date_str}")
        return date_str, True, remaining

    # --- Layer 1: dateutil (structured dates) ---
    # Guard: skip dateutil for inputs that it would mis-parse
    #  - pure digits / decimals ("42" → year 2042, "10.5" → Jan 10)
    #  - too short without a date separator (single tokens like "Dec")
    _has_date_sep = any(c in first_line for c in '-/ ')
    _looks_numeric = re.fullmatch(r'-?\d+\.?\d*', first_line) is not None
    _skip_dateutil = _looks_numeric or (len(first_line) <= 5 and not _has_date_sep)
    if not _skip_dateutil:
        try:
            fake_default = datetime(_FAKE_YEAR, 1, 1)
            parsed = dateutil_parse(first_line, default=fake_default, fuzzy=False)

            if parsed == fake_default:
                raise ValueError("no date info")

            # Reject years before 1900 or after 2100 (likely garbage parse)
            year = parsed.year if parsed.year != _FAKE_YEAR else now.year
            if not (1900 <= year <= 2100):
                raise ValueError("year out of plausible range")

            year_supplied = parsed.year != _FAKE_YEAR
            if not year_supplied:
                parsed = parsed.replace(year=now.year)

            # Cross-year heuristic: if the parsed date is more than 6 months
            # in the future and the user did NOT supply an explicit year,
            # assume they meant last year (e.g. "December 25" typed in January)
            if not year_supplied:
                delta = (parsed - now.replace(tzinfo=None) if now.tzinfo
                         else parsed - now)
                if delta.days > 183:
                    parsed = parsed.replace(year=parsed.year - 1)

            date_str = parsed.strftime('%Y-%m-%d')
            remaining = '\n'.join(lines[1:]).strip()
            log(f"Custom date detected (dateutil): {date_str}")
            return date_str, True, remaining
        except (ValueError, OverflowError):
            pass

    # --- Layer 2: parsedatetime (natural language) ---
    # Require at least 2 tokens or a known keyword to avoid false positives
    # on single abbreviations like "Dec", "Mon", "Fri"
    _pdt_keywords = {
        'yesterday', 'today', 'tomorrow', 'now',
        'ago', 'last', 'next', 'this', 'previous',
    }
    _tokens = first_line.lower().split()
    _has_decimal = any(re.fullmatch(r'-?\d+\.\d+', t) for t in _tokens)
    _try_pdt = (
        (len(_tokens) >= 2 or bool(set(_tokens) & _pdt_keywords))
        and len(_tokens) <= 4  # skip long sentences to avoid false positives
        and not _has_decimal  # skip if a token looks like a monetary amount (e.g. 6.16)
    )
    if _try_pdt:
        now_naive = now.replace(tzinfo=None) if now.tzinfo else now
        result, context = _pdt_calendar.parseDT(first_line, sourceTime=now_naive)
        if context.hasDate:
            if (result - now_naive).days > 28:
                result = result.replace(year=result.year - 1)
            date_str = result.strftime('%Y-%m-%d')
            remaining = '\n'.join(lines[1:]).strip()
            log(f"Custom date detected (parsedatetime): {date_str}")
            return date_str, True, remaining

    return now.strftime('%Y-%m-%d'), False, text



ACCOUNTS_CACHE_TTL = get_int_config("ACCOUNTS_CACHE_TTL", 300)
DRAFT_TTL_SECONDS = get_int_config("DRAFT_TTL_SECONDS", 120)

ACCOUNT_TYPE_MAP = {
    "assets": "accounts/assets.bean",
    "liabilities": "accounts/liabilities.bean",
    "equity": "accounts/equity.bean",
    "income": "accounts/income.bean",
    "expenses": "accounts/expenses.bean",
}

GITHUB_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.object",
    "X-GitHub-Api-Version": "2022-11-28"
}

_DIRECTIVE_HEADER_RE = re.compile(r'^\d{4}-\d{2}-\d{2} ')


def extract_all_directive_blocks(content: str) -> list[tuple[str, str]]:
    """Returns list of (date_str, directive_block_text) for all directives, in file order.

    Each block includes any leading ';' comment lines immediately before the directive header.
    """
    lines = content.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        if _DIRECTIVE_HEADER_RE.match(lines[i]):
            date_str = lines[i][:10]
            # Look backward for leading ';' comment lines
            comment_start = i
            while comment_start > 0 and lines[comment_start - 1].startswith(';'):
                comment_start -= 1
            # Find block end: continuation lines are indented or blank
            block_end = i + 1
            while block_end < len(lines):
                line = lines[block_end]
                if line.strip() == '':
                    block_end += 1
                elif line[0] in (' ', '\t'):
                    block_end += 1
                else:
                    break
            # Trim trailing blank lines from block
            while block_end > i + 1 and lines[block_end - 1].strip() == '':
                block_end -= 1
            directive_text = '\n'.join(lines[comment_start:block_end])
            blocks.append((date_str, directive_text))
            i = block_end
        else:
            i += 1
    return blocks


def extract_last_directive_block(content: str) -> tuple[str, str] | None:
    """Returns (directive_block_text, new_file_content) or None if no directive found."""
    lines = content.splitlines()
    last_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if _DIRECTIVE_HEADER_RE.match(lines[i]):
            last_idx = i
            break
    if last_idx is None:
        return None
    # Look backward for leading ';' comment lines
    comment_start = last_idx
    while comment_start > 0 and lines[comment_start - 1].startswith(';'):
        comment_start -= 1
    # Find block end: stop at next col-0 non-blank line
    block_end = last_idx + 1
    while block_end < len(lines):
        line = lines[block_end]
        if line.strip() == '':
            block_end += 1
        elif line[0] not in (' ', '\t'):
            break
        else:
            block_end += 1
    # Trim trailing blank lines from block
    while block_end > last_idx + 1 and lines[block_end - 1].strip() == '':
        block_end -= 1
    directive_text = '\n'.join(lines[comment_start:block_end])
    # Remove block + its leading blank separator
    remove_start = comment_start
    if remove_start > 0 and lines[remove_start - 1].strip() == '':
        remove_start -= 1
    new_lines = lines[:remove_start] + lines[block_end:]
    new_content = '\n'.join(new_lines).rstrip('\n') + '\n'
    return directive_text, new_content


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
        self._pending_lock = threading.Lock()
        self._accounts_cache = {"accounts": None, "currencies": {}, "comments": {}, "ts": 0}
        self._accounts_cache_lock = threading.Lock()
        self._file_etag_cache = {}  # file_path -> {"etag": str, "content": str, "sha": str}
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

    def parse_accounts(self):
        now = time.time()
        with self._accounts_cache_lock:
            if self._accounts_cache["accounts"] is not None and now - self._accounts_cache["ts"] < ACCOUNTS_CACHE_TTL:
                return self._accounts_cache["accounts"]

        list_headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        url = f"{GITHUB_URL_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/accounts?ref={BRANCH_NAME}"
        r = requests.get(url, headers=list_headers, timeout=30)
        if r.status_code != 200:
            log(f"Error fetching accounts: {r.status_code}")
            log(r.text)
            return []
        bean_items = [item for item in r.json() if item["name"].endswith(".bean")]

        def fetch_account_file(item):
            file_r = requests.get(item["url"], headers=list_headers, timeout=30)
            if file_r.status_code != 200:
                return {}, []
            content = base64.b64decode(file_r.json()["content"]).decode("utf-8")
            opened = {}
            closed = []
            for line in content.splitlines():
                parts = line.split()
                if len(parts) < 3:
                    continue
                date, directive, account = parts[0], parts[1], parts[2]
                if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
                    continue
                if directive == 'open':
                    # 4th field (if present and looks like a currency code) is default currency
                    currency = parts[3] if len(parts) >= 4 and re.match(r'^[A-Z][A-Z0-9]{0,9}$', parts[3]) else None
                    # extract inline comment after ';' as human-readable alias
                    comment = line.split(';', 1)[1].strip() if ';' in line else None
                    opened[account] = (currency, comment)
                elif directive == 'close':
                    closed.append(account)
            return opened, closed

        all_opened = {}
        all_closed = set()
        with ThreadPoolExecutor(max_workers=min(8, len(bean_items) or 1)) as pool:
            futures = {pool.submit(fetch_account_file, item): item for item in bean_items}
            for future in as_completed(futures):
                opened, closed = future.result()
                all_opened.update(opened)
                all_closed.update(closed)

        currencies = {k: v[0] for k, v in all_opened.items() if k not in all_closed and v[0]}
        comments = {k: v[1] for k, v in all_opened.items() if k not in all_closed and v[1]}
        accounts = sorted(k for k in all_opened if k not in all_closed)
        with self._accounts_cache_lock:
            self._accounts_cache["accounts"] = accounts
            self._accounts_cache["currencies"] = currencies
            self._accounts_cache["comments"] = comments
            self._accounts_cache["ts"] = now
        return accounts

    def _accounts_for_prompt(self) -> list[str]:
        """Return account list with default currency and comment annotations for use in LLM prompts."""
        accounts = self._accounts_cache.get("accounts") or []
        currencies = self._accounts_cache.get("currencies") or {}
        comments = self._accounts_cache.get("comments") or {}
        result = []
        for a in accounts:
            entry = a
            if a in currencies:
                entry += f" ({currencies[a]})"
            if a in comments:
                entry += f" ; {comments[a]}"
            result.append(entry)
        return result

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
        # Remove code fence markers (```lang or ```) but only the marker line itself
        # Use [ \t]* instead of \s* to avoid crossing line boundaries
        cleaned = re.sub(r'^[ \t]*```\w*[ \t]*$', '', stripped, flags=re.MULTILINE).strip()
        # Extract beancount entry: find the transaction header and collect from there
        # Use [ \t]* instead of \s* to avoid matching across line boundaries
        header_re = re.compile(r'^[ \t]*\d{4}-\d{2}-\d{2}\s+[*!txn]', re.MULTILINE)
        m = header_re.search(cleaned)
        if m:
            # Collect leading ; comment lines immediately before the header
            before = cleaned[:m.start()]
            comments = []
            for line in reversed(before.splitlines()):
                if line.strip().startswith(';'):
                    comments.insert(0, line)
                elif line.strip() == '':
                    continue
                else:
                    break
            # Collect header + all subsequent indented/posting/comment lines
            after = cleaned[m.start():]
            entry_lines = []
            for i, line in enumerate(after.splitlines()):
                if i == 0:
                    entry_lines.append(line.strip())
                elif line.strip() == '' or line[0] in (' ', '\t') or line.strip().startswith(';'):
                    entry_lines.append(line)
                else:
                    break
            return "\n".join(comments + entry_lines).strip()
        return cleaned

    def normalize_and_validate_llm_entry(self, entry_text: str, accounts: list[str]) -> str:
        text = self.strip_code_fence(entry_text)
        raw_lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        if len(raw_lines) < 3:
            raise ValueError("LLM output is too short. Expected a transaction header and at least two postings.")

        # Skip leading ; comment lines to find the header
        header_idx = 0
        leading_comments = []
        for idx, line in enumerate(raw_lines):
            if line.strip().startswith(';'):
                leading_comments.append(line.strip())
            else:
                header_idx = idx
                break

        header = raw_lines[header_idx].strip()
        # Validate header looks like a beancount directive (YYYY-MM-DD ...)
        if not re.match(r'^\d{4}-\d{2}-\d{2}\s+', header):
            raise ValueError(f"LLM output invalid: first line is not a beancount directive header: {header!r}")

        metadata_lines = [f"  {c}" if not c.startswith('  ') else c for c in leading_comments]
        postings = []

        posting_re = re.compile(r'^\s*(\S+)\s+(-?\d+(?:\.\d+)?)\s+(\S+)(?:\s+(.*))?$')
        # Beancount metadata: key-value (e.g. "  key: value") or inline comments ("; ...")
        metadata_re = re.compile(r'^\s*(\w[\w-]*\s*:.*|;.*)$')

        # Strip parenthesized currency/alias annotations that LLMs sometimes copy
        # from the account list (e.g. "Assets:Bank:CMB (CNY)" → "Assets:Bank:CMB")
        paren_annotation_re = re.compile(r'\s+\([^)]*\)(?=\s)')

        for line in raw_lines[header_idx + 1:]:
            line = paren_annotation_re.sub('', line)
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

            # Only keep valid beancount metadata/comment lines; skip natural language
            if metadata_re.match(line):
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

            if c0 == c1 and abs(a0 + a1) > 0.0001:
                raise ValueError(f"LLM output invalid: same-currency postings are unbalanced ({a0} + {a1} != 0).")

            if c0 != c1:
                has_cost_or_price = any(('@' in r or '{' in r) for r in [r0, r1])
                if not has_cost_or_price:
                    # Auto-insert FX price annotation when LLM misses @/@@ on cross-currency postings.
                    abs0, abs1 = abs(a0), abs(a1)
                    if abs0 == 0 and abs1 == 0:
                        raise ValueError("LLM output invalid: zero amounts in cross-currency postings.")

                    # Annotation always goes on the more-valuable-currency posting (smaller absolute
                    # amount), expressing how much of the cheaper currency 1 unit of the dearer one
                    # buys.  Use @ (unit price) when rate has ≤2 decimal places, otherwise @@ (total).
                    def _fx_annotation(rate_str: str, total_str: str, currency: str) -> str:
                        decimals = len(rate_str.split('.')[1]) if '.' in rate_str else 0
                        if decimals <= 2:
                            return f" @ {rate_str} {currency}"
                        return f" @@ {total_str} {currency}"

                    if abs0 <= abs1 and abs0 != 0:
                        rate = abs1 / abs0
                        rate_str = f"{rate:.8f}".rstrip('0').rstrip('.')
                        postings[0]["rest"] = (postings[0]["rest"] + _fx_annotation(rate_str, postings[1]["amount"].lstrip('-'), c1)).strip()
                    elif abs1 != 0:
                        rate = abs0 / abs1
                        rate_str = f"{rate:.8f}".rstrip('0').rstrip('.')
                        postings[1]["rest"] = (postings[1]["rest"] + _fx_annotation(rate_str, postings[0]["amount"].lstrip('-'), c0)).strip()
                    else:
                        # abs0 > 0, abs1 == 0: degenerate cross-currency posting; cannot infer FX rate.
                        raise ValueError(
                            "LLM output invalid: one cross-currency posting has zero amount; cannot infer FX rate."
                        )

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

    def validate_beancount_syntax(self, entry_text: str) -> str | None:
        """Validate entry with the beancount parser. Returns error string or None."""
        entries, errors, _ = beancount_parser.parse_string(entry_text)
        if errors:
            return "; ".join(e.message for e in errors)
        if not entries:
            return "No valid beancount entry parsed"
        return None

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

        has_datetime = any(re.match(r'^\s*datetime\s*:\s*".*"\s*$', line) for line in lines)
        if has_datetime:
            return entry_text

        # Insert datetime after the transaction header, even when leading comment lines exist.
        header_idx = None
        for idx, line in enumerate(lines):
            if re.match(r'^\d{4}-\d{2}-\d{2}\s+[*!]\s+', line.strip()) or re.match(r'^\d{4}-\d{2}-\d{2}\s+txn\s+', line.strip()):
                header_idx = idx
                break

        if header_idx is None:
            header_idx = 0

        return "\n".join(lines[: header_idx + 1] + [f'  datetime: "{datetime_str}"'] + lines[header_idx + 1 :])

    def insert_prompt_metadata(self, entry_text: str, user_input: str) -> str:
        normalized = " ".join((user_input or "").strip().splitlines()).strip()
        if not normalized:
            return entry_text

        lines = entry_text.splitlines()
        if any(re.match(r'^\s*prompt\s*:', line) for line in lines):
            return entry_text

        escaped = normalized.replace('"', '\\"')
        metadata_line = f'  prompt: "{escaped}"'

        header_idx = None
        for idx, line in enumerate(lines):
            if re.match(r'^\d{4}-\d{2}-\d{2}\s+[*!]\s+', line.strip()) or re.match(r'^\d{4}-\d{2}-\d{2}\s+txn\s+', line.strip()):
                header_idx = idx
                break

        if header_idx is None:
            return entry_text

        return "\n".join(lines[:header_idx + 1] + [metadata_line] + lines[header_idx + 1:])

    def _call_llm_backends(self, payload: dict, log_prefix: str = "", vision: bool = False) -> str:
        last_error: Exception | None = None
        for backend in LLM_BACKENDS:
            model = backend.get("vision_model", backend["model"]) if vision else backend["model"]
            try:
                url = f"{backend['base_url']}/chat/completions"
                headers = {
                    "Authorization": f"Bearer {backend['api_key']}",
                    "Content-Type": "application/json",
                }
                response = requests.post(url, headers=headers, json={**payload, "model": model}, timeout=60)
                response.raise_for_status()
                return response.json()["choices"][0]["message"]["content"].strip()
            except Exception as e:
                log(f"LLM backend '{model}'{log_prefix} failed: {e}, trying next...")
                last_error = e
        raise ValueError(f"All LLM backends failed. Last error: {last_error}")

    def call_openai_compatible(
        self,
        user_input: str,
        accounts: list[str],
        txn_date: str,
        previous_draft: str | None = None,
        decline_reason: str | None = None,
        current_time: str = "",
    ) -> str:
        if not self.llm_enabled:
            raise ValueError(self.llm_unavailable_message())

        accounts_for_prompt = self._accounts_for_prompt()
        syntax_error = None
        entry = None

        for attempt in range(1 + MAX_BEANCOUNT_RETRIES):
            if attempt == 0:
                prompt_draft = previous_draft
                prompt_reason = decline_reason
            else:
                prompt_draft = entry
                prompt_reason = f"Beancount syntax error: {syntax_error}"

            user_prompt = build_user_prompt(txn_date, accounts_for_prompt, user_input, prompt_draft, prompt_reason, current_time)
            payload = {
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": BEANCOUNT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            }

            raw_text = self._call_llm_backends(payload)

            if raw_text.upper().startswith("NEED_ACCOUNT:"):
                guidance = raw_text.split(":", 1)[1].strip() if ":" in raw_text else ""
                raise ValueError(guidance or "请在输入中提供至少一个账户名（或账户后缀），我才能生成分录。")

            try:
                entry = self.normalize_and_validate_llm_entry(raw_text, accounts)
            except Exception as e:
                raise ValueError(f"{e}\nInvalid LLM output:\n{raw_text}") from e

            syntax_error = self.validate_beancount_syntax(entry)
            if syntax_error is None:
                return entry

            log(f"Beancount syntax validation failed (attempt {attempt + 1}/{1 + MAX_BEANCOUNT_RETRIES}): {syntax_error}")

        raise ValueError(f"Beancount syntax validation failed after {MAX_BEANCOUNT_RETRIES} retries: {syntax_error}")

    def get_telegram_file_bytes(self, file_id: str) -> bytes | None:
        r = requests.get(self.api_base + "/getFile", params={"file_id": file_id}, timeout=30)
        if r.status_code != 200:
            log(f"Error getting file info: {r.status_code}")
            return None
        file_path = r.json()["result"]["file_path"]
        token = config["TELEGRAM_BOT_TOKEN"]
        dl = requests.get(f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=60)
        if dl.status_code != 200:
            log(f"Error downloading file: {dl.status_code}")
            return None
        return dl.content

    def _call_vision_with_retry(
        self, image_bytes: bytes, accounts: list[str],
        system_prompt: str, base_prompt: str, temperature: float, log_label: str,
    ) -> str:
        if not self.llm_enabled:
            raise ValueError(self.llm_unavailable_message())

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        syntax_error = None
        entry = None

        for attempt in range(1 + MAX_BEANCOUNT_RETRIES):
            if attempt == 0:
                prompt_text = base_prompt
            else:
                prompt_text = (
                    f"{base_prompt}\n\n"
                    f"Previous draft had beancount syntax errors:\n{entry}\n\n"
                    f"Error: {syntax_error}\n\n"
                    "Fix the syntax errors and regenerate."
                )

            payload = {
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                            {"type": "text", "text": prompt_text},
                        ],
                    },
                ],
            }

            raw_text = self._call_llm_backends(payload, f" {log_label}", vision=True)

            try:
                entry = self.normalize_and_validate_llm_entry(raw_text, accounts)
            except Exception as e:
                raise ValueError(f"{e}\nInvalid LLM output:\n{raw_text}") from e

            syntax_error = self.validate_beancount_syntax(entry)
            if syntax_error is None:
                return entry

            log(f"Beancount syntax validation failed (attempt {attempt + 1}/{1 + MAX_BEANCOUNT_RETRIES}): {syntax_error}")

        raise ValueError(f"Beancount syntax validation failed after {MAX_BEANCOUNT_RETRIES} retries: {syntax_error}")

    def call_openai_vision_invest(self, image_bytes: bytes, accounts: list[str], txn_date: str, caption: str = "", current_time: str = "") -> str:
        return self._call_vision_with_retry(
            image_bytes, accounts, INVEST_ORDER_SYSTEM_PROMPT,
            build_invest_order_prompt(txn_date, self._accounts_for_prompt(), caption, current_time),
            temperature=0.1, log_label="vision",
        )

    def call_openai_vision_expense(self, image_bytes: bytes, accounts: list[str], txn_date: str, caption: str = "", current_time: str = "") -> str:
        return self._call_vision_with_retry(
            image_bytes, accounts, EXPENSE_SCREENSHOT_SYSTEM_PROMPT,
            build_expense_screenshot_prompt(txn_date, self._accounts_for_prompt(), caption, current_time),
            temperature=0.2, log_label="vision-expense",
        )

    def handle_photo_message(self, message):
        msg = message["message"]
        chat_id = msg["chat"]["id"]
        caption = msg.get("caption", "").strip()

        def reply(text: str):
            self.send_message(chat_id, text)

        if CHAT_ID and str(chat_id) != CHAT_ID:
            log(f"Ignoring message from chat_id {chat_id}, only responding to {CHAT_ID}.")
            reply("How dare you?")
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
        time_str = dt.strftime('%H:%M')

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
                appendix = self.call_openai_vision_invest(image_bytes, accounts, date_str, caption, time_str)
                draft_label, user_input = "Investment order draft", caption or "(investment order screenshot)"
                commit_prefix = 'Add investment entry by Telegram Bot\n\n'
            else:
                log(f"Processing expense screenshot (caption: {caption!r})")
                appendix = self.call_openai_vision_expense(image_bytes, accounts, date_str, caption, time_str)
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

            log(f"{draft_label}:\n" + appendix)
            self.send_message(
                chat_id,
                f"{draft_label}:\n"
                f"<pre><code>{html.escape(appendix)}</code></pre>\n"
                "Use ✅ to save, 🔧 to provide feedback, or ❌ to discard.",
                reply_markup=self.build_review_buttons(pending_id),
                parse_mode="HTML",
            )
        except Exception as e:
            log(f"Photo processing failed: {e}")
            reply(f"Failed to process screenshot: {e}")

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
        quoted = re.findall(r'"([^"]*)"', header_line)
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
        self.send_message(
            chat_id,
            f"撤回最后一条指令？\n<pre><code>{html.escape(directive_text)}</code></pre>",
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
        truncated = False
        if len(text) > 4000:
            text = text[:4000]
            truncated = True
        msg = f"最近 {len(last_blocks)} 条记录：\n<pre><code>{html.escape(text)}</code></pre>"
        if truncated:
            msg += "\n（内容过长，已截断显示）"
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
        self.send_message(
            chat_id,
            f"今天（{today}）共 {len(today_blocks)} 条记录：\n<pre><code>{html.escape(text)}</code></pre>",
            parse_mode="HTML",
        )

    def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        data = {"chat_id": chat_id, "text": text}
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup is not None:
            data["reply_markup"] = reply_markup
        response = requests.post(self.api_base + "/sendMessage", json=data, timeout=30)
        if response.status_code != 200:
            log(f"Error sending message: {response.status_code}")
            log(response.text)
        return response.json()

    def answer_callback_query(self, callback_query_id, text=None):
        data = {"callback_query_id": callback_query_id}
        if text:
            data["text"] = text
        requests.post(self.api_base + "/answerCallbackQuery", json=data, timeout=30)

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        data = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": reply_markup or {"inline_keyboard": []},
        }
        requests.post(self.api_base + "/editMessageReplyMarkup", json=data, timeout=30)

    def _make_pending_entry(self, chat_id: int, appendix: str, commit_message: str, user_input: str, date_str: str) -> dict:
        return {
            "chat_id": chat_id,
            "appendix": appendix,
            "commit_message": commit_message,
            "created_at": time.time(),
            "user_input": user_input,
            "date_str": date_str,
        }

    def next_pending_id(self) -> str:
        with self._pending_lock:
            self.pending_llm_id += 1
            return str(self.pending_llm_id)

    def _pop_pending(self, pending_id: str) -> dict | None:
        """Atomically claim (pop) a pending entry. Returns None if already claimed."""
        with self._pending_lock:
            return self.pending_llm_entries.pop(pending_id, None)

    def remove_decline_reason_bindings(self, pending_id: str):
        with self._pending_lock:
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
        with self._pending_lock:
            expired_ids = [
                pending_id
                for pending_id, pending in self.pending_llm_entries.items()
                if self.is_pending_expired(pending)
            ]
            expired_entries = {pid: self.pending_llm_entries.pop(pid) for pid in expired_ids}
            for pid in expired_ids:
                for reason_chat_id, reason_pending_id in list(self.pending_decline_reasons.items()):
                    if reason_pending_id == pid:
                        self.pending_decline_reasons.pop(reason_chat_id, None)

        for pending_id, pending in expired_entries.items():
            chat_id = pending.get("chat_id")
            if chat_id is not None:
                self.send_message(chat_id, f"Draft expired after {DRAFT_TTL_SECONDS} seconds and was discarded.")

    def build_review_buttons(self, pending_id: str):
        return {
            "inline_keyboard": [[
                {"text": "✅", "callback_data": f"approve:{pending_id}"},
                {"text": "🔧", "callback_data": f"decline_reason:{pending_id}"},
                {"text": "❌", "callback_data": f"discard:{pending_id}"},
            ]]
        }

    def build_undo_buttons(self, pending_id: str):
        return {
            "inline_keyboard": [[
                {"text": "✅ 确认撤回", "callback_data": f"undo_confirm:{pending_id}"},
                {"text": "❌ 取消",     "callback_data": f"undo_cancel:{pending_id}"},
            ]]
        }

    def run_recheck(self, chat_id: int, pending_id: str, decline_reason: str | None = None):
        with self._pending_lock:
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
            self._pop_pending(pending_id)
            self.send_message(chat_id, "No accounts available. Please check GitHub account parsing first.")
            return

        try:
            new_appendix = self.call_openai_compatible(
                pending["user_input"],
                accounts,
                pending["date_str"],
                previous_draft=pending["appendix"],
                decline_reason=decline_reason,
                current_time=datetime.now(self.timezone).strftime('%H:%M'),
            )
            new_appendix = self.insert_prompt_metadata(new_appendix, pending["user_input"])
            new_commit_message = self.add_non_pnl_accounts_to_commit_message(
                'Add entry by Telegram Bot\n\n', new_appendix
            )

            new_pending_id = self.next_pending_id()
            with self._pending_lock:
                self.pending_llm_entries[new_pending_id] = self._make_pending_entry(
                    chat_id, new_appendix, new_commit_message, pending["user_input"], pending["date_str"]
                )
                self.pending_llm_entries.pop(pending_id, None)

            log("LLM rechecked draft:\n" + new_appendix)
            self.send_message(
                chat_id,
                "LLM rechecked draft:\n"
                f"<pre><code>{html.escape(new_appendix)}</code></pre>\n"
                "Use ✅ to save, 🔧 to provide feedback, or ❌ to discard.",
                reply_markup=self.build_review_buttons(new_pending_id),
                parse_mode="HTML",
            )
        except Exception as e:
            self._pop_pending(pending_id)
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

        # Atomically validate and (for destructive actions) claim the pending entry
        # so concurrent callbacks for the same pending_id cannot double-process it.
        with self._pending_lock:
            pending = self.pending_llm_entries.get(pending_id)
            if not pending:
                self.answer_callback_query(callback_id, "This request is expired or already handled")
                return

            if self.is_pending_expired(pending):
                self.pending_llm_entries.pop(pending_id, None)
                for rc, rp in list(self.pending_decline_reasons.items()):
                    if rp == pending_id:
                        self.pending_decline_reasons.pop(rc, None)
                self.answer_callback_query(callback_id, "Expired")
                self.send_message(chat_id, f"Draft expired after {DRAFT_TTL_SECONDS} seconds and was discarded.")
                return

            if chat_id != pending["chat_id"]:
                self.answer_callback_query(callback_id, "Not allowed")
                return

            if action in ("discard", "approve", "undo_confirm", "undo_cancel"):
                # Claim the entry now; prevents any concurrent thread from also processing it.
                pending = self.pending_llm_entries.pop(pending_id, None)
                if not pending:
                    self.answer_callback_query(callback_id, "This request is expired or already handled")
                    return
            elif action == "decline_reason":
                self.pending_decline_reasons[chat_id] = pending_id

        self.edit_message_reply_markup(chat_id, message_id)

        if action == "decline_reason":
            log(f"User requested recheck for pending {pending_id}")
            if not self.llm_enabled:
                self.answer_callback_query(callback_id, "LLM unavailable")
                self.send_message(chat_id, self.llm_unavailable_message())
                return

            self.answer_callback_query(callback_id, "Please send reason")
            self.send_message(chat_id, "Please send your decline reason as plain text. I will send it to LLM for recheck.")
            return

        if action == "discard":
            log(f"User discarded pending {pending_id}")
            self.answer_callback_query(callback_id, "Discarded")
            self.send_message(chat_id, "Discarded. Entry was not saved.")
            return

        if action == "undo_cancel":
            log(f"User cancelled undo for pending {pending_id}")
            self.answer_callback_query(callback_id, "已取消")
            self.send_message(chat_id, "已取消，未作任何更改。")
            return

        if action == "undo_confirm":
            log(f"User confirmed undo for pending {pending_id}")
            ok = self.github_upload_file(pending["new_content"], pending["file_sha"], pending["commit_message"])
            if ok:
                self.answer_callback_query(callback_id, "已撤回")
                self.send_message(
                    chat_id,
                    f"已撤回以下指令：\n<pre><code>{html.escape(pending['transaction_text'])}</code></pre>",
                    parse_mode="HTML",
                )
                log("Undo committed. Removed:\n" + pending["transaction_text"])
            else:
                self.answer_callback_query(callback_id, "失败")
                self.send_message(chat_id, "Failed to upload to GitHub.")
            return

        if action != "approve":
            self.answer_callback_query(callback_id, "Unknown action")
            return

        log(f"User approved pending {pending_id}")
        f = self.github_download_file()
        if not f:
            self.answer_callback_query(callback_id, "Failed")
            self.send_message(chat_id, "Failed to download from GitHub.")
            return

        appendix = pending["appendix"]
        approve_datetime_str = datetime.now(self.timezone).isoformat(timespec='seconds')
        appendix = self.ensure_datetime_metadata(appendix, approve_datetime_str)
        commit_message = pending["commit_message"]
        ok = self.github_upload_file(f["content"] + '\n' + appendix + '\n', f["sha"], commit_message.strip())

        if ok:
            self.answer_callback_query(callback_id, "Approved")
            self.send_message(chat_id, f"Created entry:\n<pre><code>{html.escape(appendix)}</code></pre>", parse_mode="HTML")
            log("Logged entry:\n" + appendix)
        else:
            self.answer_callback_query(callback_id, "Failed")
            self.send_message(chat_id, "Failed to upload to GitHub.")

    def github_download_file(self, file_path: str = FILE_PATH) -> dict | None:
        url = f"{GITHUB_URL_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{file_path}?ref={BRANCH_NAME}"
        headers = dict(GITHUB_HEADERS)
        cached = self._file_etag_cache.get(file_path)
        if cached:
            headers["If-None-Match"] = cached["etag"]
        r = requests.get(url=url, headers=headers, timeout=30)
        if r.status_code == 304 and cached:
            return {"content": cached["content"], "sha": cached["sha"]}
        if r.status_code == 200:
            data = r.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            sha = data["sha"]
            etag = r.headers.get("ETag", "")
            if etag:
                self._file_etag_cache[file_path] = {"etag": etag, "content": content, "sha": sha}
            return {"content": content, "sha": sha}
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
        r = requests.put(url=url, headers=GITHUB_HEADERS, json=data, timeout=30)
        if r.status_code in [200, 201]:
            self._file_etag_cache.pop(file_path, None)
            return True
        else:
            log(f"Error uploading file: {r.status_code}")
            log(r.text)
            return False

    def github_trigger_workflow(self, workflow_file: str, inputs: dict) -> tuple[bool, str]:
        url = f"{GITHUB_URL_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/actions/workflows/{workflow_file}/dispatches"
        data = {"ref": BRANCH_NAME, "inputs": inputs}
        r = requests.post(url=url, headers=GITHUB_HEADERS, json=data, timeout=30)
        if r.status_code == 204:
            return True, ""
        else:
            error = f"{r.status_code} {r.text}"
            log(f"Error triggering workflow: {error}")
            return False, error

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
        time_str = dt.strftime('%H:%M')
        datetime_str = dt.isoformat(timespec='seconds')

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

        commit_message = 'Add entry by Telegram Bot\n\n'
        appendix = ""
        target_file_path = FILE_PATH

        if text.startswith('/'):
            text = text[1:]
            command = text.split(' ', 1)[0]
            payload = text[len(command):].strip()
            log(f"Command: {command}, Payload: {payload}")
            if command == "tz":
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

        elif text.lower().startswith("open"):
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
            target_file_path = ACCOUNT_TYPE_MAP.get(prefix, FILE_PATH)
            appendix = jinja2.get_template("open.bean.j2").render(
                date=date_str, account=account, currency=currency, datetime=datetime_str
            )

        elif text.lower().startswith("close"):
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
            target_file_path = ACCOUNT_TYPE_MAP.get(prefix, FILE_PATH)
            appendix = jinja2.get_template("close.bean.j2").render(
                date=date_str, account=account, datetime=datetime_str
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
            # balance assertions take effect on the *opening* of the stated date,
            # so the default is tomorrow (today + 1 day). Override by prefixing
            # the message with a YYYY-MM-DD date line.
            balance_date_str = date_str if custom_date else (dt + timedelta(days=1)).strftime('%Y-%m-%d')
            appendix = jinja2.get_template("balance.bean.j2").render(
                date=balance_date_str, account=account, amount=amount, currency=currency, datetime=datetime_str
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
            try:
                appendix = self.call_openai_compatible(text, accounts, date_str, current_time="" if custom_date else time_str)
                appendix = self.insert_prompt_metadata(appendix, text)
                commit_message = self.add_non_pnl_accounts_to_commit_message(commit_message, appendix)

                pending_id = self.next_pending_id()
                with self._pending_lock:
                    self.pending_llm_entries[pending_id] = self._make_pending_entry(
                        chat_id, appendix, commit_message, text, date_str
                    )

                log("LLM draft:\n" + appendix)
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
                    if abs(a0 + a1) > 0.0001:
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
            response = requests.get(self.api_base + "/getUpdates", params=params, timeout=params["timeout"] + 1)
            if response.status_code != 200:
                log(f"Error: {response.status_code}")
                return {"result": []}
        except KeyboardInterrupt:
            log("Got KeyboardInterrupt in Bot thread.")
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
            log(updates)

        for message in edited_messages:
            self.update_id = message["update_id"]
            log(message)

        for callback in callback_queries:
            self.update_id = callback["update_id"]
            hd = threading.Thread(target=self.handle_callback_query, args=(callback,), daemon=True)
            hd.start()

        for message in messages:
            self.update_id = message["update_id"]
            chat = message["message"]["chat"]
            chat_id = chat["id"]
            first_name = chat.get("first_name", "")
            last_name = chat.get("last_name", "")
            username = chat.get("username", "")
            text = message["message"].get("text")

            fmt = f"{_C_BLUE}[{chat_id}]{_C_RESET} {first_name} {last_name} (@{username}):"
            photo = message["message"].get("photo")
            if text:
                hd = threading.Thread(target=self.handle_message, args=(message,), daemon=True)
                hd.start()
                if len(text.splitlines()) > 1:
                    log(f"{fmt} \n{text}")
                else:
                    log(f"{fmt} {text}")
            elif photo:
                log(f"{fmt} [photo]")
                hd = threading.Thread(target=self.handle_photo_message, args=(message,), daemon=True)
                hd.start()
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
