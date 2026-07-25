# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the bot
.venv/bin/python main.py

# Run in debug mode (extra logging)
.venv/bin/python main.py debug

# Run tests (no pytest — use unittest directly). CI runs all three on push/PR
# via .github/workflows/tests.yml; they import main with builtins.open patched,
# so they need no config.json.
.venv/bin/python tests/test_refactor.py   # core logic tests
.venv/bin/python tests/test_bot.py        # Bot behaviour: auth, polling resilience, drafts, queries
.venv/bin/python tests/test_fuzz.py       # fuzzing / edge-case tests

# Interactive LLM test tool
.venv/bin/python tests/test_llm.py
```

## Architecture

**Python Telegram Bot (`main.py`)** — the only backend
- Polls Telegram for updates; spawns a daemon thread per message
- `Bot` class holds all state: pending drafts, account cache, LLM config
- Per-message flow: text → LLM → pending draft → user confirm/decline → GitHub commit
- Photo messages (investment screenshots) go through a vision LLM path (`call_openai_vision_invest`)

### Key modules
- `main.py` — bot entry point, `Bot` class (~1700 lines)
- `prompts.py` — LLM system prompts and user prompt builders for both text and vision paths
- `templates/*.bean.j2` — Jinja2 templates for beancount directives (`open`, `close`, `balance`, `pad`, `transaction`)

### Config (`config.json`, gitignored)
Required keys: `GITHUB_TOKEN`, `REPO_OWNER`, `REPO_NAME`, `BRANCH_NAME`, `FILE_PATH`, `TIMEZONE`, `TELEGRAM_BOT_TOKEN`, `CHAT_ID`

LLM backends (array preferred, single-backend keys for backward compat):
```json
"LLM_BACKENDS": [{"LLM_API_BASE_URL": "...", "LLM_API_KEY": "...", "LLM_MODEL": "..."}]
```

Optional tuning: `ACCOUNTS_CACHE_TTL` (default 300s), `DRAFT_TTL_SECONDS` (default 120s)

### Authorization (default-deny)
`CHAT_ID` is a single chat id or a comma-separated whitelist, parsed at import into the
`ALLOWED_CHATS` set. It is **required**: `Bot.__init__` raises if it is empty, because an
empty value would let anyone who finds the bot read the ledger and commit with the owner's
`GITHUB_TOKEN`. All three entry points (`handle_message`, `handle_photo_message`,
`handle_callback_query`) gate on `is_authorized(chat_id)` and drop unauthorized traffic
silently — no reply, so the bot does not confirm its own existence to strangers.
`process_updates` logs a sender's name/username/text **before** that gate runs (in the spawned
handler), so those attacker-controlled fields pass through `_scrub()` (strips `\x00-\x1f\x7f`)
to stop a stranger injecting ANSI/newline escapes into the operator's console.

### Beancount storage (GitHub)
- `main.bean` — top-level file (value of `FILE_PATH`)
- `accounts/{assets,liabilities,equity,income,expenses}.bean` — account definitions; fetched in parallel via `ThreadPoolExecutor` and cached for `ACCOUNTS_CACHE_TTL` seconds
- `ACCOUNT_TYPE_MAP` module constant maps lowercase prefix → bean file path

### LLM backend fallback
`LLM_BACKENDS` list is tried in order; `Bot._call_llm_backends(payload)` raises `ValueError` if all fail. Text and vision paths both use this helper. LLM output starting with `NEED_ACCOUNT:` signals missing account context and is surfaced as a user-facing error.

### Polling resilience
`get_updates` backs off (1→2→4…→60s, `stop.wait` so Ctrl-C interrupts) on any non-200,
network error, or malformed payload instead of returning at once and spinning; 429 honours
the server's `retry_after`. `process_updates` acks the whole batch at its max `update_id`
(the three per-type loops used to each assign it, rewinding past a trailing callback).
`_spawn_handler` wraps every handler thread so a crash reports back to the user (authorized
only — a crash must not become a liveness oracle) instead of dying silently; `start()`
catches a crashing cycle and backs off rather than exiting.

### Appending to the ledger
`append_to_file(appendix, commit_message, file_path, downloaded=None)` is the single
download→append→upload path (approve, manual entries). GitHub rejects a PUT with a stale
sha (409/422) whenever two entries land close together; it drops the cached ETag, re-reads,
and re-appends up to `GITHUB_CONFLICT_RETRIES` (3) — safe because appends commute. `approve`
passes the file it already fetched via `downloaded` to skip a round trip, and on failure
`_restore_pending()` puts the reviewed draft back (fresh TTL) with its buttons intact so one
tap retries. `/undo` does **not** use this: it rewrites the whole file, so a 409 means its
precomputed content is stale and the user is told to re-run `/undo`. `_github_put_file()`
returns `(ok, status)`; `github_upload_file()` is a bool wrapper.

### Conversational queries (NL → BQL)
Single-line text first goes through `route_intent()`, one temperature-0 LLM call that
classifies entry-vs-query and, for a query, emits the BQL in the same response
(`QUERY_ROUTER_SYSTEM_PROMPT`). It **fails toward `entry`** on any doubt. `load_ledger()`
concatenates the account files (open/close only) with the journal and feeds
`loader.load_string()` — beancount's loader wants a path on disk but the ledger is on GitHub.
`run_bql()` runs it; `answer_query()` wraps the same feed-error-back-to-LLM retry loop as
syntax validation (a download failure is not retried). Queries are read-only (no draft), and
`format_query_result()` sizes columns by `display_width()` (CJK=2) because beancount's own
renderer miscounts. Prompt gotcha: `units`/`cost` are functions, not columns.

### Payee context for drafts
When the router classifies input as an entry, two best-effort lookups enrich the draft prompt
so the LLM matches the user's own conventions. `handle_message` calls `load_ledger()` **once**
and passes the result to both helpers via their optional `loaded=` param, so they don't each
pay a separate GitHub round trip for the same tree. Both **never block entry generation** — any
failure logs and falls back to generating without the context.
- **Same-payee history** (`examples_for_payee`): if the router named a `payee`, the user's
  most recent past transactions for that merchant (loose case-folded substring match, either
  direction) are rendered back to beancount text via `_format_example_entry` (header + postings
  only, no metadata) and shown so the LLM reuses the account/narration/currency conventions.
- **Frequent-payee list** (`frequent_payees`): the top 50 payees by frequency across the whole
  ledger are handed to the LLM so it snaps a fuzzy input onto an existing merchant spelling
  instead of coining a near-duplicate. The ranked list is cached in `_ledger_cache["payees"]`
  keyed by tree sha (recomputed only when the ledger changes, guarded by an `entries is entries`
  identity check against a concurrent reload). Interior whitespace is collapsed so a multi-line
  payee stays a single `、`-joined token.

Both feed into `build_user_prompt(..., examples, payees)`, threaded through `call_openai_compatible`.

### Beancount syntax validation
After `normalize_and_validate_llm_entry()`, every LLM-generated entry is validated with `beancount.parser.parser.parse_string()`. If the parser reports errors, the entry + error message are sent back to the LLM for correction, up to `MAX_BEANCOUNT_RETRIES` (3) retries. Both text (`call_openai_compatible`) and vision (`call_openai_vision_invest`) paths use this retry loop.

### Pending draft lifecycle
1. LLM generates entry → beancount syntax validated (with auto-retry) → stored in `Bot.pending_llm_entries` with `_make_pending_entry()`
2. Bot sends entry text + inline confirm/decline/edit buttons to user
3. On confirm → GitHub commit; on decline → discard; on no action → expires after `DRAFT_TTL_SECONDS` and `cleanup_expired_drafts()` notifies user

On confirm the approve path downloads first, then claims (pops) the entry, then commits via
`append_to_file`; buttons are stripped only after the write succeeds, and a failed commit
restores the draft (fresh TTL) so one tap retries. See "Appending to the ledger".

Undo entries also use `pending_llm_entries` with `"kind": "undo"` to distinguish them from LLM draft entries. They store `new_content` and `file_sha` pre-computed at show-time.

### /undo command
- `/undo` — previews and removes the last beancount directive from `main.bean` (any top-level directive: transaction, balance, pad, open, close)
- `extract_last_directive_block(content)` — module-level pure function; scans backward for last `YYYY-MM-DD ` line, includes leading `;` comment lines in the removed block, returns `(directive_text, new_file_content)`
- Callback actions: `undo_confirm:<id>` commits `new_content` to GitHub; `undo_cancel:<id>` discards

### /last and /today commands
- `/last [N]` — shows the last N directives from `main.bean` (default 5, max 50); output truncated at 4000 chars for Telegram message limit. As a backstop, `send_message` truncates any plain-text body to `TELEGRAM_MESSAGE_LIMIT` (4096) so an over-long error can't 400 the whole POST and vanish; HTML callers pre-truncate their own payload and are left untouched. It also guards `response.json()` so a non-JSON error body (e.g. a proxy's HTML 502) can't raise and mask the failure it was reporting.
- `/today` — shows all directives matching today's date (timezone-aware via `self.timezone`)
- Both use `extract_all_directive_blocks(content)` — module-level pure function that returns `[(date_str, block_text), ...]` in file order; each block includes leading `;` comment lines
- Read-only commands; no pending entry or confirmation flow

### GitHub file ETag caching
- `Bot._file_etag_cache` — dict keyed by file path, stores `{"etag", "content", "sha"}`
- `github_download_file()` sends `If-None-Match` header when cache exists; on `304 Not Modified`, returns cached content without re-downloading
- `github_upload_file()` invalidates the cache entry on success to ensure next read fetches fresh data

### LLM output sanitization
- `strip_code_fence()` — removes markdown code fences, then extracts the beancount entry by finding the `YYYY-MM-DD` transaction header line; discards any surrounding natural language (important for recheck flow where LLMs sometimes return conversational responses)
- `normalize_and_validate_llm_entry()` — validates header is a beancount directive, strips parenthesized annotations like `(GBP)` that LLMs copy from account lists, filters metadata lines with strict regex (only `key: value` and `;` comments), rejects natural language; also handles balance validation, cross-currency FX annotation, and `:Current` suffix resolution
- Key regex constraint: all `re.MULTILINE` patterns use `[ \t]*` (not `\s*`) to avoid crossing line boundaries

### User input recording
After LLM generation, `insert_prompt_metadata()` inserts the original user text as a `prompt` metadata field directly after the transaction header line:
```beancount
2026-04-17 * "星巴克" "咖啡"
  prompt: "今天买了一杯咖啡 35 元"
  datetime: "14:30"
  Expenses:Food:Coffee  35 CNY
  Assets:WeChat:Current
```
- Text messages: always inserted; photo messages: inserted only when caption is present
- Double quotes in the prompt value are escaped as `\"`
- Idempotent: skips insertion if `prompt:` key already exists
- Falls back to no-op if no transaction header line is found (e.g. balance directives)

### Account list prompt format
Accounts are sent to the LLM with annotations: `Assets:Bank:CMB (CNY) ; 招商��行`. The `(currency)` and `; alias` parts are for LLM context only — the sanitizer strips them if the LLM copies them into postings. Built by `_accounts_for_prompt`.

### Date handling
`parse_natural_date(text, now)` extracts an optional date from the first line of user input using a three-layer fallback:
1. **Chinese keywords** — prefix-matched `昨天`, `前天`, `上周五`, `3天前`, etc.
2. **dateutil** — structured dates (`2024-03-15`, `March 15`, `3/15`); guarded against pure-numeric and very short inputs
3. **parsedatetime** — relative English dates (`yesterday`, `last friday`, `3 days ago`); guarded against long sentences and inputs containing decimal amounts (e.g. `6.16`) to prevent monetary values from being misinterpreted as dates

If a date is detected, it overrides today's date and the first line is stripped from the input before LLM call.

### Thread safety (Python bot)
- `_pending_lock` — protects `pending_llm_entries` and `pending_decline_reasons` dicts
- `_accounts_cache_lock` — protects `_accounts_cache` read/write (network calls run outside the lock)
- `print_lock` — serializes log output
- Each message/callback is processed in its own daemon thread

### Input validation
- `/open` validates account name against beancount pattern (`^[A-Z][a-zA-Z0-9]*(?::[A-Z][a-zA-Z0-9]*)+$`) and currency against `^[A-Z][A-Z0-9]{0,9}$`
- `/update` validates amount is numeric
- Manual transaction postings validate currency format

### GitHub Actions workflows (`.github/workflows/*.yml.example`)
- `monthly-report.yml.example` — daily Sankey chart of monthly expenses sent to Telegram; configurable `REPORT_CURRENCY` and `FX_RATES` (JSON dict) at workflow `env` level; aggregates sub-accounts into top-level categories
- `notify-on-push.yml.example` — on push to main: sends expense breakdown + affected account balances to Telegram; reads account names from commit body
