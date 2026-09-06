"""Shared pure helpers, constants and HTTP transport (no credential loading)."""

import html
import json
import os
import re
import threading
import time
import unicodedata
from decimal import Decimal
from datetime import date as date_cls, datetime, timedelta
from pprint import pformat
from contextlib import contextmanager

import parsedatetime as pdt
from dateutil.parser import parse as dateutil_parse
import requests
from requests.adapters import HTTPAdapter
from jinja2 import Environment, FileSystemLoader


MAX_BEANCOUNT_RETRIES = 3
NOT_LOADED = object()


@contextmanager
def timed(stage):
    started = time.monotonic()
    try:
        yield
    finally:
        log(f"Timing [{stage}]: {time.monotonic() - started:.3f}s")

# Rounding slack when checking that a transaction's postings sum to zero.
BALANCE_TOLERANCE = Decimal("0.0001")

# Re-reads to attempt when GitHub rejects a write because the file moved under us.
GITHUB_CONFLICT_RETRIES = 3

# Polling backoff: a failing getUpdates returns immediately instead of blocking for the
# long-poll timeout, so without this the loop spins and hammers the API.
POLL_BACKOFF_BASE = 1.0
POLL_BACKOFF_MAX = 60.0


class AccountMatchError(ValueError):
    """A suffix identifies more than one account."""


class LedgerValidationError(ValueError):
    """A generated draft failed ledger validation; its message is user-facing."""


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


class _ThreadHTTP:
    """Keep connection pooling without sharing mutable requests.Session state."""
    def __init__(self):
        self.local = threading.local()

    def __getattr__(self, name):
        if not hasattr(self.local, "session"):
            self.local.session = _build_http_session()
        return getattr(self.local.session, name)


HTTP = _ThreadHTTP()

GITHUB_URL_BASE = "https://api.github.com"
FILE_PATH = "main.bean"  # Default only; instances use Settings.FILE_PATH.

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



ACCOUNTS_CACHE_TTL = 300
DRAFT_TTL_SECONDS = 120

ACCOUNT_TYPE_MAP = {
    "assets": "accounts/assets.bean",
    "liabilities": "accounts/liabilities.bean",
    "equity": "accounts/equity.bean",
    "income": "accounts/income.bean",
    "expenses": "accounts/expenses.bean",
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
