import base64
import html
import json
import os
import shutil
import tempfile
import re
import sys
import threading
import time
import traceback
import unicodedata
from collections import Counter
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as date_cls, datetime, timedelta
from pprint import pformat

import parsedatetime as pdt
import pytz
from dateutil.parser import parse as dateutil_parse
import requests
from requests.adapters import HTTPAdapter
from beancount import loader as beancount_loader
from beancount.core.data import Transaction
from beancount.parser import parser as beancount_parser
from beancount.query import query as beancount_query
from jinja2 import Environment, FileSystemLoader

from prompts import (
    BEANCOUNT_SYSTEM_PROMPT, build_user_prompt,
    QUERY_ROUTER_SYSTEM_PROMPT, build_query_router_prompt,
    INVEST_ORDER_SYSTEM_PROMPT, build_invest_order_prompt,
    EXPENSE_SCREENSHOT_SYSTEM_PROMPT, build_expense_screenshot_prompt,
)

MAX_BEANCOUNT_RETRIES = 3

# Rounding slack when checking that a transaction's postings sum to zero.
BALANCE_TOLERANCE = 0.0001

# Re-reads to attempt when GitHub rejects a write because the file moved under us.
GITHUB_CONFLICT_RETRIES = 3

# Polling backoff: a failing getUpdates returns immediately instead of blocking for the
# long-poll timeout, so without this the loop spins and hammers the API.
POLL_BACKOFF_BASE = 1.0
POLL_BACKOFF_MAX = 60.0


def _build_http_session() -> requests.Session:
    """Shared session reused for all outbound HTTP (LLM, GitHub, Telegram).

    Connection pooling avoids a fresh TLS handshake on every call. No automatic
    retries are configured on purpose: the LLM layer relies on fast failover to
    the next backend, which urllib3-level retries would delay.
    """
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


HTTP = _build_http_session()

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
# A single id, or a comma-separated whitelist. Empty means nobody — Bot.__init__ refuses
# to start rather than serving whoever finds the bot.
ALLOWED_CHATS = {s.strip() for s in str(config.get("CHAT_ID") or "").split(",") if s.strip()}


def is_authorized(chat_id) -> bool:
    return str(chat_id) in ALLOWED_CHATS


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

# Absolute so the bot and tests work regardless of the working directory (systemd, cron,
# `python tests/…`) rather than only when launched from the repo root.
_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
jinja2 = Environment(loader=FileSystemLoader(searchpath=_TEMPLATES_DIR))


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


def _scrub(value) -> str:
    """Strip control chars (ANSI escapes, CR/LF, etc.) from untrusted text before it
    reaches the operator's terminal. process_updates logs a sender's name/username/text
    *before* the authorization check, so any stranger who finds the bot could otherwise
    inject escape sequences to forge or hide console log lines."""
    return re.sub(r"[\x00-\x1f\x7f]", "?", str(value))


def _is_account_error(message: str) -> bool:
    """True when an LLM-generation error is really 'a needed account is missing', which the
    user can fix — so we surface the guidance verbatim instead of a generic failure notice."""
    return bool(message) and ("账户" in message or "account" in message.lower())


_FAKE_YEAR = 9999
_pdt_consts = pdt.Constants(usePyICU=False)
_pdt_consts.DOWParseStyle = -1  # "Monday" → today or the *last* Monday
_pdt_calendar = pdt.Calendar(_pdt_consts, version=pdt.VERSION_CONTEXT_STYLE)

# Chinese relative-date keywords, matched by prefix against the first line of input. These
# are constants, so build them once at import rather than on every inbound text message.
_CHINESE_DATE_MAP = {
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
_CN_DAY_NAMES = {'一': 0, '二': 1, '三': 2, '四': 3,
                 '五': 4, '六': 5, '日': 6, '天': 6}


def _join_remaining(rest_of_first: str, lines: list) -> str:
    return "\n".join(([rest_of_first] if rest_of_first else []) + lines[1:]).strip()


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
    for _kw, _delta in _CHINESE_DATE_MAP.items():
        if first_line.startswith(_kw):
            d = now + _delta
            date_str = d.strftime('%Y-%m-%d')
            rest_of_first = first_line[len(_kw):].strip()
            remaining = _join_remaining(rest_of_first, lines)
            log(f"Custom date detected (Chinese keyword): {date_str}")
            return date_str, True, remaining
    _chinese_weekday_re = re.match(
        r'^(上+|下+)(?:周|星期|礼拜)([一二三四五六日天])\s*(.*)',
        first_line, re.DOTALL)
    if _chinese_weekday_re:
        prefix, day_char, rest_of_first = _chinese_weekday_re.groups()
        target_wd = _CN_DAY_NAMES[day_char]
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
        remaining = _join_remaining(rest_of_first, lines)
        log(f"Custom date detected (Chinese weekday): {date_str}")
        return date_str, True, remaining
    # Check for "N天前" / "N天后" pattern
    _chinese_ago_re = re.match(r'^(\d+)\s*天前\s*(.*)', first_line, re.DOTALL)
    _chinese_later_re = re.match(r'^(\d+)\s*天后\s*(.*)', first_line, re.DOTALL)
    # Bound the day count to avoid OverflowError (timedelta/date out of range)
    # crashing the bare daemon thread that runs handle_message. Absurd values
    # like "9999999999天前" fall through to the later date layers / no-date path.
    if _chinese_ago_re and int(_chinese_ago_re.group(1)) <= 100000:
        days = int(_chinese_ago_re.group(1))
        rest_of_first = _chinese_ago_re.group(2).strip()
        d = now - timedelta(days=days)
        date_str = d.strftime('%Y-%m-%d')
        remaining = _join_remaining(rest_of_first, lines)
        log(f"Custom date detected (Chinese N天前): {date_str}")
        return date_str, True, remaining
    if _chinese_later_re and int(_chinese_later_re.group(1)) <= 100000:
        days = int(_chinese_later_re.group(1))
        rest_of_first = _chinese_later_re.group(2).strip()
        d = now + timedelta(days=days)
        date_str = d.strftime('%Y-%m-%d')
        remaining = _join_remaining(rest_of_first, lines)
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
        # nlp() reports the matched span, unlike parseDT which only returns the date and
        # forced the caller to drop the whole first line — "2026-03-20 星巴克咖啡" reached
        # the LLM as an empty prompt with the description silently lost. Keeping the
        # non-date part is what the Chinese layers above already do via _join_remaining.
        # VERSION_CONTEXT_STYLE (see _pdt_calendar) makes the second field a pdtContext,
        # so filter on .hasDate — a bare "3pm" carries a time but no date.
        matches = _pdt_calendar.nlp(first_line, sourceTime=now_naive) or []
        date_match = next((m for m in matches if m[1].hasDate), None)
        if date_match:
            result, _ctx, start, end, _matched = date_match
            if (result - now_naive).days > 28:
                result = result.replace(year=result.year - 1)
            date_str = result.strftime('%Y-%m-%d')
            rest_of_first = (first_line[:start] + first_line[end:]).strip()
            log(f"Custom date detected (parsedatetime): {date_str}")
            return date_str, True, _join_remaining(rest_of_first, lines)

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

_TXN_HEADER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s+(?:[*!]|txn)\s+")


def _is_txn_header(line: str) -> bool:
    return bool(_TXN_HEADER_RE.match(line.strip()))


def _code_block(text: str) -> str:
    return f"<pre><code>{html.escape(text)}</code></pre>"


# Telegram caps a message at 4096 chars; leave room for the <pre> wrapper and a notice.
QUERY_RESULT_MAX_CHARS = 3500
# Telegram's hard per-message limit; send_message truncates plain text to fit under it.
TELEGRAM_MESSAGE_LIMIT = 4096


def _utf16_len(s: str) -> int:
    """Telegram measures a message against its 4096 cap in UTF-16 code units, so an astral
    char (most emoji) counts as 2, not 1. Python's len() counts code points; use this wherever
    a Telegram length cap is enforced, or an emoji near the limit slips over it and 400s."""
    return len(s.encode("utf-16-le")) // 2


def _capped_code_block(text: str, budget: int) -> tuple[str, bool]:
    """Return (html_code_block, truncated) whose UTF-16 length is <= `budget`.

    html.escape only ever grows the string (a quote-heavy beancount line can nearly double),
    so truncating the raw text before escaping — as callers used to — can still overflow past
    the limit once wrapped, silently 400ing the whole HTML message. Here we shrink the raw
    text until the *escaped, wrapped* result fits, measured in UTF-16 units (what Telegram
    counts), so it can't reflow over budget."""
    truncated = False
    while True:
        block = _code_block(text)
        overshoot = _utf16_len(block) - budget
        if overshoot <= 0 or not text:
            return block, truncated
        text = text[:-max(overshoot, 1)]
        truncated = True


def extract_json_object(text: str) -> dict | None:
    """Pull the first JSON object out of an LLM reply, tolerating code fences and prose.

    Scans for a balanced {...} while respecting string literals, so a brace inside a
    BQL string (e.g. a regex) does not end the object early. Returns None if there is
    no parseable object — callers treat that as "router gave up".
    """
    if not text:
        return None

    cleaned = text.strip()
    fence = re.match(r'^```[a-zA-Z]*[ \t]*\n(.*?)\n?```$', cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()

    start = cleaned.find('{')
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(cleaned)):
        c = cleaned[i]
        if in_string:
            if escaped:
                escaped = False
            elif c == '\\':
                escaped = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(cleaned[start:i + 1])
                except ValueError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def display_width(text: str) -> int:
    """Width in monospace cells. CJK and fullwidth characters take two.

    beancount's own query_render measures in characters, which renders Chinese
    narrations misaligned in Telegram's <pre> block.
    """
    return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)


# BQL column names (and common AS aliases) → labels shown to the user, so the reply
# reads as a table of "商家/摘要/金额" rather than "payee/narration/sum_position".
_COLUMN_LABELS = {
    "date": "日期", "year": "年", "month": "月", "day": "日",
    "account": "账户", "payee": "商家", "narration": "摘要",
    "position": "金额", "number": "金额", "currency": "币种",
    "balance": "余额", "flag": "标记", "tags": "标签", "links": "链接",
    "total": "总额", "monthly_avg": "月均", "average": "平均", "avg": "平均",
}


def _friendly_header(name: str) -> str:
    if name in _COLUMN_LABELS:
        return _COLUMN_LABELS[name]
    low = name.lower()
    if low.startswith("count"):
        return "笔数"
    if low.startswith("sum"):
        return "合计"
    if low.startswith(("first", "last", "min", "max")):
        return _COLUMN_LABELS.get(low.split("_", 1)[-1], name)
    return name  # an LLM-chosen alias is usually already readable


def _format_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, date_cls) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return ",".join(sorted(str(v) for v in value))
    if isinstance(value, Decimal):
        # Amounts, averages, ratios: round to 2 dp with thousands separators so a
        # monthly average shows 21.67, not 21.66666666666666666666666667.
        return f"{value:,.2f}"
    return str(value)


def format_query_result(rtypes, rrows, max_chars: int = QUERY_RESULT_MAX_CHARS) -> str:
    """Render BQL rows as a width-aware fixed-column table, truncated to fit Telegram."""
    if not rrows:
        return "没有找到匹配的记录。"

    headers = [_friendly_header(name) for name, _ in rtypes]
    rows = [[_format_cell(v) for v in row] for row in rrows]

    widths = []
    for i, header in enumerate(headers):
        widths.append(max([display_width(header)] + [display_width(r[i]) for r in rows]))

    def render(cells):
        return "  ".join(
            cell + " " * max(0, widths[i] - display_width(cell))
            for i, cell in enumerate(cells)
        ).rstrip()

    head = [render(headers), "  ".join("-" * w for w in widths)]
    body = [render(r) for r in rows]

    out = "\n".join(head + body)
    if len(out) <= max_chars:
        return out

    kept = []
    size = len("\n".join(head))
    for line in body:
        if size + len(line) + 1 > max_chars - 60:
            break
        kept.append(line)
        size += len(line) + 1
    dropped = len(body) - len(kept)
    return "\n".join(head + kept + [f"... ({dropped} more rows omitted)"])


def _directive_block_span(lines: list[str], header_idx: int) -> tuple[int, int]:
    """Given the index of a directive header line, return (comment_start, block_end).

    `comment_start` extends backward over any leading ';' comment lines; `block_end` is the
    exclusive end after the header's continuation lines (indented or blank), with trailing
    blank lines trimmed off. Shared by extract_all/extract_last so the two can't drift."""
    comment_start = header_idx
    while comment_start > 0 and lines[comment_start - 1].startswith(';'):
        comment_start -= 1
    block_end = header_idx + 1
    while block_end < len(lines):
        line = lines[block_end]
        if line.strip() == '' or line[0] in (' ', '\t'):
            block_end += 1
        else:
            break
    while block_end > header_idx + 1 and lines[block_end - 1].strip() == '':
        block_end -= 1
    return comment_start, block_end


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
            comment_start, block_end = _directive_block_span(lines, i)
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
    comment_start, block_end = _directive_block_span(lines, last_idx)
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
        if not ALLOWED_CHATS:
            raise ValueError(
                "CHAT_ID is required: an empty value would let anyone who finds the bot read "
                "the ledger and commit to the repo. Set it in config.json (comma-separated for "
                "multiple chats). Find yours via https://api.telegram.org/bot<TOKEN>/getUpdates"
            )
        self.update_id = 0
        self.debug = debug
        self.stop = threading.Event()
        self._poll_failures = 0
        self.timezone = pytz.timezone(config["TIMEZONE"])
        self.api_base = "https://api.telegram.org/bot{}".format(config["TELEGRAM_BOT_TOKEN"])
        self.pending_llm_entries = {}
        self.pending_llm_id = 0
        self.pending_decline_reasons = {}
        self._pending_lock = threading.Lock()
        self._accounts_cache = {"accounts": None, "currencies": {}, "comments": {}, "ts": 0, "sha_map": None}
        self._accounts_cache_lock = threading.Lock()
        self._file_etag_cache = {}  # file_path -> {"etag": str, "content": str, "sha": str}
        # Parsed ledger cached by repo tree sha; reused until any .bean file changes.
        self._ledger_cache = {"tree_sha": None, "entries": None, "options_map": None}
        self._ledger_cache_lock = threading.Lock()
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
        r = HTTP.get(url, headers=list_headers, timeout=30)
        if r.status_code != 200:
            log(f"Error fetching accounts: {r.status_code}")
            log(r.text)
            return []
        bean_items = [item for item in r.json() if item["name"].endswith(".bean")]

        # Conditional refresh: the cheap directory listing already tells us the
        # sha of every account file. If the full name->sha map is byte-for-byte
        # identical to what we last parsed, the cached parsed accounts are still
        # valid, so we skip the per-file downloads + reparse. Full-dict equality
        # (not per-file sha matching) is required so that file additions AND
        # deletions both invalidate the cache and we never serve stale accounts.
        new_map = {item["name"]: item["sha"] for item in bean_items}
        with self._accounts_cache_lock:
            if self._accounts_cache["accounts"] is not None and self._accounts_cache.get("sha_map") == new_map:
                self._accounts_cache["ts"] = now
                return self._accounts_cache["accounts"]

        def fetch_account_file(item):
            file_r = HTTP.get(item["url"], headers=list_headers, timeout=30)
            if file_r.status_code != 200:
                return {}, [], False
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
            return opened, closed, True

        all_opened = {}
        all_closed = set()
        fetch_ok = True
        with ThreadPoolExecutor(max_workers=min(8, len(bean_items) or 1)) as pool:
            futures = {pool.submit(fetch_account_file, item): item for item in bean_items}
            for future in as_completed(futures):
                opened, closed, ok = future.result()
                if not ok:
                    fetch_ok = False
                all_opened.update(opened)
                all_closed.update(closed)

        currencies = {k: v[0] for k, v in all_opened.items() if k not in all_closed and v[0]}
        comments = {k: v[1] for k, v in all_opened.items() if k not in all_closed and v[1]}
        accounts = sorted(k for k in all_opened if k not in all_closed)
        with self._accounts_cache_lock:
            self._accounts_cache["accounts"] = accounts
            self._accounts_cache["currencies"] = currencies
            self._accounts_cache["comments"] = comments
            # Only lock in the sha map when every file fetched cleanly; caching a
            # partial parse's map would serve incomplete accounts for the whole TTL.
            self._accounts_cache["sha_map"] = new_map if fetch_ok else None
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
        # Keys must start with [a-z] per beancount spec.
        metadata_re = re.compile(r'^\s*([a-z][a-zA-Z0-9_-]*\s*:.*|;.*)$')

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
            if abs(total) > BALANCE_TOLERANCE:
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

            if c0 == c1 and abs(a0 + a1) > BALANCE_TOLERANCE:
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
                token = m.group(1)
                # Skip metadata lines (key: value) — only real posting lines start with an account name.
                if re.match(r'^[A-Z][A-Za-z0-9]*(?::[A-Z][A-Za-z0-9\-]*)+$', token):
                    accounts.append(token)
        return accounts

    def validate_accounts_exist(self, entry_text: str, accounts: list[str]) -> str | None:
        """Check every posting account in entry_text exists in the known accounts list."""
        known = set(accounts)
        used = self.extract_accounts_from_entry(entry_text)
        missing = [a for a in used if a not in known]
        if missing:
            # De-duplicate preserving order.
            seen = set()
            unique_missing = [a for a in missing if not (a in seen or seen.add(a))]
            return f"Unknown account(s): {', '.join(unique_missing)}"
        return None

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
            if _is_txn_header(line):
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

        escaped = normalized.replace('\\', '\\\\').replace('"', '\\"')
        metadata_line = f'  prompt: "{escaped}"'

        header_idx = None
        for idx, line in enumerate(lines):
            if _is_txn_header(line):
                header_idx = idx
                break

        if header_idx is None:
            return entry_text

        return "\n".join(lines[:header_idx + 1] + [metadata_line] + lines[header_idx + 1:])

    def _list_bean_files(self) -> tuple[str, list[str]] | None:
        """List every .bean/.beancount path in the repo via the git trees API.

        Returns (tree_sha, paths), or None on failure so the caller can fall back. The
        tree sha changes iff any file changes, so load_ledger uses it as a cache key.
        This is also what lets the loader resolve `include` globs (e.g.
        `include "config/*.bean"`): every file the glob could match is fetched, not just
        the accounts/ files we know by name.
        """
        url = f"{GITHUB_URL_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/git/trees/{BRANCH_NAME}?recursive=1"
        headers = {"Authorization": f"token {GITHUB_TOKEN}",
                   "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        r = HTTP.get(url, headers=headers, timeout=30)
        if r.status_code != 200:
            log(f"Could not list repo tree: HTTP {r.status_code}")
            return None
        data = r.json()
        if data.get("truncated"):
            log("Repo tree is truncated; some ledger files may be missing.")
        paths = [t["path"] for t in data.get("tree", [])
                 if t.get("type") == "blob" and t["path"].endswith((".bean", ".beancount"))]
        return data.get("sha"), paths

    def load_ledger(self) -> tuple[list, dict] | None:
        """Fetch the whole ledger and parse it with beancount's file loader.

        beancount's loader wants a path on disk and resolves `include` (including globs)
        relative to it, but the ledger lives in GitHub. So we mirror every .bean file into
        a temp dir preserving its path, then load_file(FILE_PATH) — this is the only way to
        honor arbitrary include directives. Downloads reuse github_download_file (ETag cache).
        """
        listed = self._list_bean_files()
        if listed is None:
            # Fallback when the trees API is unavailable: at least the files we know by
            # name. No tree sha, so this path is never served from cache.
            tree_sha, paths = None, list(ACCOUNT_TYPE_MAP.values())
        else:
            tree_sha, paths = listed
        if FILE_PATH not in paths:
            paths = paths + [FILE_PATH]

        if tree_sha is not None:
            with self._ledger_cache_lock:
                if self._ledger_cache["tree_sha"] == tree_sha:
                    return self._ledger_cache["entries"], self._ledger_cache["options_map"]

        texts = {}
        with ThreadPoolExecutor(max_workers=min(8, len(paths))) as pool:
            futures = {pool.submit(self.github_download_file, p): p for p in paths}
            for future in as_completed(futures):
                f = future.result()
                if f:
                    texts[futures[future]] = f["content"]

        if not texts.get(FILE_PATH):
            log("Ledger main file is empty or missing; cannot query.")
            return None

        tmpdir = tempfile.mkdtemp(prefix="ledger_")
        try:
            for rel, content in texts.items():
                dest = os.path.join(tmpdir, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w", encoding="utf-8") as fh:
                    fh.write(content)
            entries, errors, options_map = beancount_loader.load_file(
                os.path.join(tmpdir, FILE_PATH))
            if errors:
                log(f"Ledger parsed with {len(errors)} error(s); first: {errors[0]}")
            if tree_sha is not None:
                with self._ledger_cache_lock:
                    self._ledger_cache = {"tree_sha": tree_sha, "entries": entries,
                                          "options_map": options_map}
            return entries, options_map
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def run_bql(self, bql: str, loaded=None) -> tuple[list, list]:
        """Execute a BQL query. Raises on a bad query; the caller feeds that back to the LLM.
        Callers retrying a query pass a cached `load_ledger()` result as `loaded` to avoid
        re-fetching and re-parsing the (unchanged) ledger on every attempt."""
        if loaded is None:
            loaded = self.load_ledger()
        if loaded is None:
            raise ValueError("Failed to download the ledger from GitHub.")
        entries, options_map = loaded
        return beancount_query.run_query(entries, options_map, bql)

    @staticmethod
    def _format_example_entry(txn) -> str:
        """Render a parsed Transaction back to beancount text (header + postings only,
        no metadata) so the LLM sees the user's own format without the noise of the
        prompt/datetime metadata this pipeline injects separately. Cost {...} and price @
        annotations are kept so commodity/FX examples stay balanced, and quotes in the
        payee/narration are escaped so the reference text is valid beancount."""
        def q(s: str | None) -> str:
            return (s or "").replace('"', '\\"')

        header = f'{txn.date.isoformat()} {txn.flag or "*"}'
        if txn.payee:
            header += f' "{q(txn.payee)}"'
        header += f' "{q(txn.narration)}"'
        lines = [header]
        for p in txn.postings:
            amount = ""
            if p.units is not None:
                amount = f"{p.units.number} {p.units.currency}"
                if p.cost is not None and getattr(p.cost, "number", None) is not None:
                    amount += f" {{{p.cost.number} {p.cost.currency}}}"
                if p.price is not None:
                    amount += f" @ {p.price.number} {p.price.currency}"
            lines.append(f"  {p.account}  {amount}".rstrip())
        return "\n".join(lines)

    def examples_for_payee(self, payee: str, limit: int = 10, loaded=None) -> str | None:
        """Return up to `limit` most recent past transactions whose payee matches,
        rendered as beancount directives, to show the LLM this user's own format for
        that merchant (route A in CLAUDE.md). Best-effort: returns None when the ledger
        can't be loaded or nothing matches — it must never block entry generation.
        Matching is loose (substring both ways, case-folded) because the payee is only
        the router's guess and may not exactly equal the stored string. Callers that
        already hold a `load_ledger()` result pass it as `loaded` to skip a round trip."""
        needle = payee.casefold().strip()
        if not needle:
            return None
        if loaded is None:
            loaded = self.load_ledger()
        if loaded is None:
            return None
        entries, _ = loaded
        matched = [
            e for e in entries
            if isinstance(e, Transaction) and e.payee
            and (needle in e.payee.casefold() or e.payee.casefold() in needle)
        ]
        if not matched:
            return None
        matched.sort(key=lambda e: e.date)
        return "\n\n".join(self._format_example_entry(e) for e in matched[-limit:])

    def frequent_payees(self, limit: int = 50, loaded=None) -> list[str]:
        """Return up to `limit` payees ordered by how often they appear in the ledger.

        Seeds the draft prompt so the LLM reuses the user's existing spelling of a
        merchant instead of inventing a near-duplicate (「星巴克」vs「Starbucks」).
        Best-effort: returns [] when the ledger can't be loaded — it must never block
        entry generation. The O(n) count is cached alongside the parsed ledger (keyed by
        tree sha), so it runs once per ledger change rather than once per draft. Callers
        that already hold a `load_ledger()` result pass it as `loaded` to skip a round trip."""
        if loaded is None:
            loaded = self.load_ledger()
        if loaded is None:
            return []
        entries, _ = loaded
        with self._ledger_cache_lock:
            cached = self._ledger_cache.get("payees")
            if cached is not None and self._ledger_cache["entries"] is entries:
                return cached[:limit]
        # Collapse interior whitespace so a payee that legally spans multiple lines in the
        # ledger stays a single token here — otherwise a newline would break the one-line,
        # 、-joined list we hand the LLM. Also merges names that differ only by whitespace.
        counts = Counter(
            norm for e in entries
            if isinstance(e, Transaction) and e.payee
            and (norm := re.sub(r"\s+", " ", e.payee).strip())
        )
        payees = [p for p, _ in counts.most_common()]
        with self._ledger_cache_lock:
            # Only stash against the ledger we actually counted; a concurrent reload may
            # have swapped the cache. The full ranked list is kept so a larger limit can
            # reuse it. The fallback (no tree sha) never caches entries, so it recomputes.
            if self._ledger_cache["entries"] is entries:
                self._ledger_cache["payees"] = payees
        return payees[:limit]

    def route_intent(self, user_input: str, today: str) -> dict:
        """Classify the input as entry-vs-query and, for a query, produce a BQL.

        Returns {"intent": "entry"} or {"intent": "query", "bql": "..."}. Falls back to
        entry on any failure: a misrouted entry costs the user one tap on ❌, while a
        misrouted query would just be a confusing draft — both recoverable, unlike
        blocking the bot's primary purpose because the router hiccuped.
        """
        payload = {
            "temperature": 0,
            "messages": [
                {"role": "system", "content": QUERY_ROUTER_SYSTEM_PROMPT},
                {"role": "user", "content": build_query_router_prompt(
                    user_input, self._accounts_for_prompt(), today)},
            ],
        }
        try:
            raw = self._call_llm_backends(payload, " router")
        except Exception as e:
            log(f"Intent router failed ({e}); treating input as an entry.")
            return {"intent": "entry"}

        parsed = extract_json_object(raw)
        if not parsed or parsed.get("intent") not in ("entry", "query"):
            log(f"Intent router returned unusable output {raw!r}; treating input as an entry.")
            return {"intent": "entry"}
        if parsed["intent"] == "query" and not parsed.get("bql"):
            log("Intent router said query but gave no BQL; treating input as an entry.")
            return {"intent": "entry"}
        if parsed["intent"] == "query":
            log(f"Query BQL from LLM: {parsed['bql']}")
        return parsed

    def answer_query(self, user_input: str, bql: str, today: str) -> tuple[str, str]:
        """Run the BQL, re-asking the LLM to fix it if beancount rejects it.

        Mirrors the beancount-syntax retry loop used for entries. Returns (bql, rendered).
        """
        accounts = self._accounts_for_prompt()
        # The ledger can't change across retries, so load it once here rather than paying a
        # fetch+parse (or, on the no-tree-sha fallback path, a full re-download) each attempt.
        loaded = self.load_ledger()
        if loaded is None:
            raise ValueError("Failed to download the ledger from GitHub.")
        error = None
        for attempt in range(1 + MAX_BEANCOUNT_RETRIES):
            try:
                rtypes, rrows = self.run_bql(bql, loaded=loaded)
                log(f"BQL ok ({len(rrows)} rows): {bql}")
                return bql, format_query_result(rtypes, rrows)
            except ValueError:
                raise  # ledger download failure — not something the LLM can fix
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                log(f"BQL failed (attempt {attempt + 1}/{1 + MAX_BEANCOUNT_RETRIES}): {error}")

            if attempt == MAX_BEANCOUNT_RETRIES:
                break

            payload = {
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": QUERY_ROUTER_SYSTEM_PROMPT},
                    {"role": "user", "content": build_query_router_prompt(
                        user_input, accounts, today, previous_bql=bql, bql_error=error)},
                ],
            }
            raw = self._call_llm_backends(payload, " bql-retry")
            parsed = extract_json_object(raw)
            if not parsed or not parsed.get("bql"):
                break
            bql = parsed["bql"]

        raise ValueError(f"Could not build a working query. Last error:\n{error}")

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
                started = time.monotonic()
                response = HTTP.post(url, headers=headers, json={**payload, "model": model}, timeout=60)
                response.raise_for_status()
                data = response.json()
                try:
                    content = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as e:
                    raise ValueError(f"Malformed LLM response: {data}") from e
                if content is None:
                    raise ValueError(f"LLM returned null content: {data}")
                elapsed = time.monotonic() - started
                log(f"LLM{log_prefix} answered by {_C_BLUE}[{model}]{_C_RESET} @ {backend['base_url']} ({elapsed:.1f}s)")
                return content.strip()
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
        examples: str | None = None,
        payees: list[str] | None = None,
    ) -> str:
        if not self.llm_enabled:
            raise ValueError(self.llm_unavailable_message())

        accounts_for_prompt = self._accounts_for_prompt()
        validation_error = None
        entry = None

        for attempt in range(1 + MAX_BEANCOUNT_RETRIES):
            if attempt == 0:
                prompt_draft = previous_draft
                prompt_reason = decline_reason
            else:
                prompt_draft = entry
                prompt_reason = f"Previous draft validation error: {validation_error}"

            user_prompt = build_user_prompt(txn_date, accounts_for_prompt, user_input, prompt_draft, prompt_reason, current_time, examples, payees)
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

            validation_error = self.validate_beancount_syntax(entry) or self.validate_accounts_exist(entry, accounts)
            if validation_error is None:
                return entry

            log(f"Draft validation failed (attempt {attempt + 1}/{1 + MAX_BEANCOUNT_RETRIES}): {validation_error}")

        raise ValueError(f"Draft validation failed after {MAX_BEANCOUNT_RETRIES} retries: {validation_error}")

    def get_telegram_file_bytes(self, file_id: str) -> bytes | None:
        r = HTTP.get(self.api_base + "/getFile", params={"file_id": file_id}, timeout=30)
        if r.status_code != 200:
            log(f"Error getting file info: {r.status_code}")
            return None
        try:
            file_path = r.json()["result"]["file_path"]
        except (ValueError, KeyError, TypeError) as e:
            # A 200 with a malformed/non-JSON body must honour this method's None contract
            # (the caller replies "Failed to download the image."), not raise past it.
            log(f"getFile returned an unexpected body: {e}")
            return None
        token = config["TELEGRAM_BOT_TOKEN"]
        dl = HTTP.get(f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=60)
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
        validation_error = None
        entry = None

        for attempt in range(1 + MAX_BEANCOUNT_RETRIES):
            if attempt == 0:
                prompt_text = base_prompt
            else:
                prompt_text = (
                    f"{base_prompt}\n\n"
                    f"Previous draft had validation errors:\n{entry}\n\n"
                    f"Error: {validation_error}\n\n"
                    "Fix the errors and regenerate."
                )

            if attempt == 0:
                user_content = [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": prompt_text},
                ]
            else:
                # Retries are driven only by textual validation errors
                # (syntax / account-exists), which the pixels rarely help fix.
                # Drop the image on retry to avoid re-billing up to 3x image
                # tokens; the full prompt text (incl. account list) is kept so
                # account-exists corrections still work.
                log(f"Retry attempt {attempt}: dropping screenshot, sending text-only correction")
                user_content = [
                    {"type": "text", "text": prompt_text},
                ]

            payload = {
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            }

            raw_text = self._call_llm_backends(payload, f" {log_label}", vision=True)

            try:
                entry = self.normalize_and_validate_llm_entry(raw_text, accounts)
            except Exception as e:
                raise ValueError(f"{e}\nInvalid LLM output:\n{raw_text}") from e

            validation_error = self.validate_beancount_syntax(entry) or self.validate_accounts_exist(entry, accounts)
            if validation_error is None:
                return entry

            log(f"Draft validation failed (attempt {attempt + 1}/{1 + MAX_BEANCOUNT_RETRIES}): {validation_error}")

        raise ValueError(f"Draft validation failed after {MAX_BEANCOUNT_RETRIES} retries: {validation_error}")

    def call_openai_vision_invest(self, image_bytes: bytes, accounts: list[str], txn_date: str, caption: str = "", current_datetime: str = "") -> str:
        return self._call_vision_with_retry(
            image_bytes, accounts, INVEST_ORDER_SYSTEM_PROMPT,
            build_invest_order_prompt(txn_date, self._accounts_for_prompt(), caption, current_datetime),
            temperature=0.1, log_label="vision",
        )

    def call_openai_vision_expense(self, image_bytes: bytes, accounts: list[str], txn_date: str, caption: str = "", current_datetime: str = "") -> str:
        return self._call_vision_with_retry(
            image_bytes, accounts, EXPENSE_SCREENSHOT_SYSTEM_PROMPT,
            build_expense_screenshot_prompt(txn_date, self._accounts_for_prompt(), caption, current_datetime),
            temperature=0.2, log_label="vision-expense",
        )

    def handle_photo_message(self, message):
        msg = message["message"]
        chat_id = msg["chat"]["id"]
        caption = msg.get("caption", "").strip()

        def reply(text: str):
            self.send_message(chat_id, text)

        if not is_authorized(chat_id):
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

            log(f"{draft_label}:\n" + appendix)
            self.send_draft_for_review(chat_id, f"{draft_label}:", appendix, pending_id)
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

    def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        # Telegram hard-caps a message at 4096 chars and 400s the whole POST if exceeded —
        # since send_message is the last hop of every reply() error path, an over-long body
        # (e.g. an error that embeds raw LLM output) would fail *silently*, leaving the user
        # with nothing. Guard plain text here; HTML callers pre-truncate their own payload
        # so we must not blind-cut mid-tag.
        if parse_mode is None and isinstance(text, str) and _utf16_len(text) > TELEGRAM_MESSAGE_LIMIT:
            # Cut by code points until the UTF-16 length (what Telegram counts) fits, leaving
            # room for the ellipsis. Over-cuts on astral chars, which is fine — it only needs
            # to get under the cap, and this path is a last-resort backstop for outsized text.
            text = text[:TELEGRAM_MESSAGE_LIMIT - 1]
            while _utf16_len(text) > TELEGRAM_MESSAGE_LIMIT - 1:
                text = text[:-1]
            text += "…"
        data = {"chat_id": chat_id, "text": text}
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup is not None:
            data["reply_markup"] = reply_markup
        response = HTTP.post(self.api_base + "/sendMessage", json=data, timeout=30)
        if response.status_code != 200:
            log(f"Error sending message: {response.status_code}")
            log(response.text)
        # No caller uses the return value; guard .json() so a non-JSON error body (e.g. an
        # HTML 502 from a proxy) can't raise here and mask the original failure that this
        # very call was trying to report.
        try:
            return response.json()
        except ValueError:
            return {}

    def answer_callback_query(self, callback_query_id, text=None):
        data = {"callback_query_id": callback_query_id}
        if text:
            data["text"] = text
        HTTP.post(self.api_base + "/answerCallbackQuery", json=data, timeout=30)

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        data = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": reply_markup or {"inline_keyboard": []},
        }
        HTTP.post(self.api_base + "/editMessageReplyMarkup", json=data, timeout=30)

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

    def _restore_pending(self, pending_id: str, pending: dict) -> dict:
        """Put a claimed entry back after a failed commit, so the user's reviewed draft
        is not lost to a transient GitHub error. The TTL clock restarts: they only just
        interacted with it, and expiring it immediately would defeat the retry."""
        pending = dict(pending, created_at=time.time())
        with self._pending_lock:
            self.pending_llm_entries[pending_id] = pending
        return pending

    def _remove_decline_reason_bindings_locked(self, pending_id):
        for k, v in list(self.pending_decline_reasons.items()):
            if v == pending_id:
                self.pending_decline_reasons.pop(k, None)

    def remove_decline_reason_bindings(self, pending_id: str):
        with self._pending_lock:
            self._remove_decline_reason_bindings_locked(pending_id)

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
                self._remove_decline_reason_bindings_locked(pid)

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

    def send_draft_for_review(self, chat_id, header, appendix, pending_id):
        self.send_message(chat_id, f"{header}\n{_code_block(appendix)}\nUse ✅ to save, 🔧 to provide feedback, or ❌ to discard.", reply_markup=self.build_review_buttons(pending_id), parse_mode="HTML")

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
            self.send_draft_for_review(chat_id, "LLM rechecked draft:", new_appendix, new_pending_id)
        except Exception as e:
            self._pop_pending(pending_id)
            log(f"LLM recheck failed: {e}")
            error_text = str(e)
            if _is_account_error(error_text):
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

        if not is_authorized(chat_id):
            log(f"Ignoring callback from unauthorized chat_id {chat_id}.")
            return

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
                self._remove_decline_reason_bindings_locked(pending_id)
                self.answer_callback_query(callback_id, "Expired")
                self.send_message(chat_id, f"Draft expired after {DRAFT_TTL_SECONDS} seconds and was discarded.")
                return

            if chat_id != pending["chat_id"]:
                self.answer_callback_query(callback_id, "Not allowed")
                return

            if action in ("discard", "undo_confirm", "undo_cancel"):
                # Claim the entry now; prevents any concurrent thread from also processing it.
                pending = self.pending_llm_entries.pop(pending_id, None)
                if not pending:
                    self.answer_callback_query(callback_id, "This request is expired or already handled")
                    return
            elif action == "decline_reason":
                self.pending_decline_reasons[chat_id] = pending_id
            # NOTE: "approve" is intentionally NOT claimed here. It defers the pop
            # (and stripping of the inline buttons) until AFTER a successful GitHub
            # download, so a transient download failure leaves the validated draft
            # and its buttons intact for the user to retry.

        if action != "approve":
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
            # An undo rewrites the whole file, so unlike an append it cannot be retried
            # against a moved sha — a conflict means the precomputed content is stale.
            ok, status = self._github_put_file(
                pending["new_content"], pending["file_sha"], pending["commit_message"])
            if ok:
                self.answer_callback_query(callback_id, "已撤回")
                self.send_message(
                    chat_id,
                    f"已撤回以下指令：\n{_code_block(pending['transaction_text'])}",
                    parse_mode="HTML",
                )
                log("Undo committed. Removed:\n" + pending["transaction_text"])
            elif status in (409, 422):
                self.answer_callback_query(callback_id, "已过期")
                self.send_message(chat_id, "账本在此期间已变更，撤回已作废。请重新执行 /undo。")
            else:
                self._restore_pending(pending_id, pending)
                self.answer_callback_query(callback_id, "失败")
                self.send_message(chat_id, "Failed to upload to GitHub. 草稿仍在，可再次点击确认重试。")
            return

        if action != "approve":
            self.answer_callback_query(callback_id, "Unknown action")
            return

        log(f"User approved pending {pending_id}")
        # Download FIRST. On failure, the pending entry and its inline buttons are
        # still intact (we have not popped or edited the message yet), so the user
        # can retry instead of losing a validated draft.
        f = self.github_download_file()
        if not f:
            self.answer_callback_query(callback_id, "Failed")
            self.send_message(chat_id, "Failed to download from GitHub.")
            return

        # The download succeeded — now claim the entry. If a concurrent approve
        # already claimed it, bail out without double-committing.
        claimed = self._pop_pending(pending_id)
        if claimed is None:
            self.answer_callback_query(callback_id, "This request is expired or already handled")
            return

        appendix = claimed["appendix"]
        approve_datetime_str = datetime.now(self.timezone).isoformat(timespec='seconds')
        appendix = self.ensure_datetime_metadata(appendix, approve_datetime_str)
        # Reuse the file we just downloaded; a concurrent commit makes the sha stale and
        # append_to_file re-reads on its own.
        ok, err = self.append_to_file(appendix, claimed["commit_message"].strip(), downloaded=f)

        if ok:
            # Strip the buttons only now. Double-taps are already prevented by the pop
            # above, so leaving them until the write lands means a failed commit can be
            # retried with a tap instead of forcing a retype.
            self.edit_message_reply_markup(chat_id, message_id)
            self.answer_callback_query(callback_id, "Approved")
            self.send_message(chat_id, f"Created entry:\n{_code_block(appendix)}", parse_mode="HTML")
            log("Logged entry:\n" + appendix)
        else:
            self._restore_pending(pending_id, claimed)
            self.answer_callback_query(callback_id, "Failed")
            self.send_message(chat_id, f"{err} 草稿仍在，可再次点击 ✅ 重试。")

    def github_download_file(self, file_path: str = FILE_PATH) -> dict | None:
        url = f"{GITHUB_URL_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{file_path}?ref={BRANCH_NAME}"
        headers = dict(GITHUB_HEADERS)
        cached = self._file_etag_cache.get(file_path)
        if cached:
            headers["If-None-Match"] = cached["etag"]
        r = HTTP.get(url=url, headers=headers, timeout=30)
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

    def _github_put_file(self, content: str, sha: str, commit_message: str,
                         file_path: str = FILE_PATH) -> tuple[bool, int]:
        """PUT a file and report the HTTP status, so callers can tell a stale-sha
        conflict (retryable) from a real failure (not)."""
        url = f"{GITHUB_URL_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{file_path}"
        data = {
            "message": commit_message,
            "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
            "branch": BRANCH_NAME,
        }
        if sha:
            data["sha"] = sha
        r = HTTP.put(url=url, headers=GITHUB_HEADERS, json=data, timeout=30)
        if r.status_code in [200, 201]:
            self._file_etag_cache.pop(file_path, None)
            return True, r.status_code
        log(f"Error uploading file: {r.status_code}")
        log(r.text)
        return False, r.status_code

    def github_upload_file(self, content: str, sha: str, commit_message: str, file_path: str = FILE_PATH) -> bool:
        ok, _ = self._github_put_file(content, sha, commit_message, file_path)
        return ok

    def append_to_file(self, appendix: str, commit_message: str, file_path: str = FILE_PATH,
                       downloaded: dict | None = None) -> tuple[bool, str]:
        """Append to a file, retrying when it changed underneath us.

        GitHub rejects a PUT carrying a stale sha (409, or 422 for the same reason),
        which is exactly what happens when two entries are approved close together.
        Re-reading and re-appending is always safe here because appends commute; the
        alternative is telling the user their reviewed entry failed for no good reason.

        `downloaded` lets a caller that already fetched the file (to validate before
        claiming a draft) hand it over instead of paying for a second round trip.
        Returns (ok, error_message_for_user).
        """
        f = downloaded
        for attempt in range(1, GITHUB_CONFLICT_RETRIES + 1):
            if f is None:
                f = self.github_download_file(file_path)
                if not f:
                    return False, "Failed to download from GitHub."

            ok, status = self._github_put_file(
                f["content"] + '\n' + appendix + '\n', f["sha"], commit_message, file_path)
            if ok:
                return True, ""
            if status not in (409, 422):
                return False, "Failed to upload to GitHub."

            # Someone else committed between our read and write. Drop the cached ETag
            # so the next read is guaranteed fresh, then rebuild the append.
            self._file_etag_cache.pop(file_path, None)
            f = None
            log(f"{file_path} changed under us (HTTP {status}); "
                f"re-reading and retrying ({attempt}/{GITHUB_CONFLICT_RETRIES})")

        return False, "The ledger is being updated by something else. Please try again."

    def github_trigger_workflow(self, workflow_file: str, inputs: dict) -> tuple[bool, str]:
        url = f"{GITHUB_URL_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/actions/workflows/{workflow_file}/dispatches"
        data = {"ref": BRANCH_NAME, "inputs": inputs}
        r = HTTP.post(url=url, headers=GITHUB_HEADERS, json=data, timeout=30)
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

        if not is_authorized(chat_id):
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
            target_file_path = ACCOUNT_TYPE_MAP.get(prefix, FILE_PATH)
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
            target_file_path = ACCOUNT_TYPE_MAP.get(prefix, FILE_PATH)
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
                    reply("查询没能完成，换个说法再试试？")
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
                appendix = self.call_openai_compatible(text, accounts, date_str, current_time="" if custom_date else time_str, examples=examples, payees=payees)
                appendix = self.insert_prompt_metadata(appendix, text)
                commit_message = self.add_non_pnl_accounts_to_commit_message(commit_message, appendix)

                pending_id = self.next_pending_id()
                with self._pending_lock:
                    self.pending_llm_entries[pending_id] = self._make_pending_entry(
                        chat_id, appendix, commit_message, text, date_str
                    )

                log("LLM draft:\n" + appendix)
                self.send_draft_for_review(chat_id, "LLM draft (checked padding):", appendix, pending_id)
                return
            except Exception as e:
                log(f"LLM generation failed: {e}")
                error_text = str(e)
                if _is_account_error(error_text):
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
                a0, a1 = float(postings[0]["amount"]), float(postings[1]["amount"])
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

    def _backoff(self, reason: str, retry_after: float | None = None):
        """Wait before polling again. Waits on `stop` so Ctrl-C interrupts the delay."""
        self._poll_failures += 1
        if retry_after is None:
            retry_after = min(POLL_BACKOFF_MAX, POLL_BACKOFF_BASE * 2 ** (self._poll_failures - 1))
        log(f"{reason}; retrying in {retry_after:.1f}s")
        self.stop.wait(retry_after)

    def get_updates(self):
        params = {"offset": self.update_id + 1, "timeout": 30}
        try:
            response = HTTP.get(self.api_base + "/getUpdates", params=params, timeout=params["timeout"] + 1)
        except KeyboardInterrupt:
            log("Got KeyboardInterrupt in Bot thread.")
            self.stop.set()
            exit(0)
        except Exception as e:
            self._backoff(f"getUpdates failed: {e}")
            return {"result": []}

        if response.status_code != 200:
            # A failing call returns at once instead of long-polling for 30s, so without
            # a delay the loop spins. 429 tells us exactly how long to wait.
            retry_after = None
            if response.status_code == 429:
                try:
                    retry_after = float(response.json()["parameters"]["retry_after"])
                except Exception:
                    pass
            self._backoff(f"getUpdates HTTP {response.status_code}", retry_after)
            return {"result": []}

        try:
            data = response.json()
        except ValueError:
            self._backoff("getUpdates returned non-JSON")
            return {"result": []}

        if not isinstance(data.get("result"), list):
            # Telegram error payloads have no "result"; indexing it would kill the loop.
            self._backoff(f"getUpdates payload has no 'result' list: {str(data)[:120]}")
            return {"result": []}

        self._poll_failures = 0
        return data

    def _spawn_handler(self, fn, update, chat_id):
        """Run a handler in a daemon thread with a top-level guard, so a crash reports
        back to the user instead of dying silently in a thread nobody is watching."""
        # Resolve the name up front: nothing inside the except block may itself raise.
        name = getattr(fn, "__name__", type(fn).__name__)

        def guarded():
            try:
                fn(update)
            except Exception:
                log(f"{name} crashed for chat {chat_id}:\n{traceback.format_exc()}")
                # Only ever reply to authorized chats — a crash must not become an oracle.
                if is_authorized(chat_id):
                    try:
                        self.send_message(chat_id, "Something went wrong handling that. Please try again.")
                    except Exception:
                        log(f"Could not notify chat {chat_id}:\n{traceback.format_exc()}")

        threading.Thread(target=guarded, daemon=True).start()

    def process_updates(self):
        self.cleanup_expired_drafts()
        updates = self.get_updates()
        edited_messages = [x for x in updates["result"] if "edited_message" in x]
        callback_queries = [x for x in updates["result"] if "callback_query" in x]
        messages = [x for x in updates["result"] if "message" in x]

        if self.debug:
            log(updates)

        if updates["result"]:
            self.update_id = max(u["update_id"] for u in updates["result"])

        for message in edited_messages:
            log(message)

        for callback in callback_queries:
            cb_chat_id = callback.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")
            self._spawn_handler(self.handle_callback_query, callback, cb_chat_id)

        for message in messages:
            chat = message["message"]["chat"]
            chat_id = chat["id"]
            # These fields are attacker-controlled and logged before is_authorized() runs
            # (inside the spawned handler), so scrub control chars out of anything that
            # reaches the terminal. See _scrub.
            first_name = _scrub(chat.get("first_name", ""))
            last_name = _scrub(chat.get("last_name", ""))
            username = _scrub(chat.get("username", ""))
            text = message["message"].get("text")

            fmt = f"{_C_BLUE}[{chat_id}]{_C_RESET} {first_name} {last_name} (@{username}):"
            photo = message["message"].get("photo")
            if text:
                self._spawn_handler(self.handle_message, message, chat_id)
                log(f"{fmt} \n{_scrub(text)}" if len(text.splitlines()) > 1 else f"{fmt} {_scrub(text)}")
            elif photo:
                log(f"{fmt} [photo]")
                self._spawn_handler(self.handle_photo_message, message, chat_id)
            else:
                obj = {k: v for k, v in message['message'].items() if k not in ['chat', 'date', 'from', 'message_id']}
                log(f"{fmt} {_scrub(obj)}")

    def start(self):
        while not self.stop.is_set():
            try:
                self.process_updates()
            except KeyboardInterrupt:
                raise
            except Exception:
                # One bad update or a network blip must not terminate the bot.
                log(f"Poll cycle crashed:\n{traceback.format_exc()}")
                self._backoff("Poll cycle crashed")


if __name__ == "__main__":
    debug = len(sys.argv) > 1 and sys.argv[1] == "debug"
    bot = Bot(debug)
    if debug:
        log("Debug mode")
    try:
        bot.start()
    except KeyboardInterrupt:
        log("Exiting...")
