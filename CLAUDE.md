# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the bot
.venv/bin/python main.py

# Run in debug mode (extra logging)
.venv/bin/python main.py debug

# Run tests (unittest). All four suites inject settings and isolated state;
# no deployment config or credentials are needed.
.venv/bin/python tests/test_refactor.py   # core logic tests
.venv/bin/python tests/test_bot.py        # Bot behaviour: auth, polling resilience, drafts, queries
.venv/bin/python tests/test_fuzz.py       # fuzzing / edge-case tests
.venv/bin/python tests/test_runtime.py    # SQLite restart, bounded queues, configuration

# Interactive LLM test tool (real services; use synthetic input and a test ledger)
.venv/bin/python scripts/preview_llm.py --live
```

## Architecture

**Python Telegram Bot (`beancounter/bot.py`)** — root `main.py` is a thin, stable launch entry point.
- Polls Telegram; persists updates before acknowledgment and uses bounded FIFO worker lanes
- `Bot` class holds all state: pending drafts, account cache, LLM config
- Per-message flow: text → LLM → pending draft → user confirm/decline → GitHub commit
- Photo messages (investment screenshots) go through a vision LLM path (`call_openai_vision_invest`)

### Key modules

- `main.py` — stable startup wrapper; also exports `Bot` for existing callers
- `beancounter/bot.py` — Bot composition, initialization and command handlers
- `beancounter/settings.py` — explicit per-instance configuration; root config, user prompt and state paths remain unchanged
- `beancounter/entries.py`, `ledger.py`, `llm.py`, `drafts.py`, `telegram_api.py` — focused Bot mixins (all inside the package)
- `beancounter/bot_utils.py` — formatting/date helpers, constants, thread-local HTTP connection pools
- `beancounter/dispatch.py`, `state_store.py` — bounded workers and SQLite state/inbox
- `beancounter/ledger_validation.py` — complete local bean-check equivalent
- `beancounter/prompts.py` — LLM system prompts and user prompt builders
- `beancounter/templates/*.bean.j2` — Jinja2 templates located relative to the package, not the working directory
- `scripts/preview_llm.py` — opt-in live integration preview, separate from offline `tests/`

Use relative imports within `beancounter` and package-qualified imports in tests and tools. Keep root `main.py` lightweight; do not move personal configuration or runtime state into the package. Module names mentioned below refer to this package unless stated otherwise. Avoid hard-wrapping documentation paragraphs.

### Config (`config.json`, gitignored)
Required keys: `GITHUB_TOKEN`, `REPO_OWNER`, `REPO_NAME`, `BRANCH_NAME`, `FILE_PATH`, `TIMEZONE`, `TELEGRAM_BOT_TOKEN`, `CHAT_ID`

LLM backends:
```json
"LLM_BACKENDS": [{"LLM_API_BASE_URL": "...", "LLM_API_KEY": "...", "LLM_MODEL": "..."}]
```

Optional tuning: `ACCOUNTS_CACHE_TTL` (300s), `DRAFT_TTL_SECONDS` (120s), `WORKERS` (4),
`QUEUE_SIZE` (64), `STATE_PATH` (`data/bot.sqlite3`), `LEDGER_ROOT` (main.bean if present,
otherwise FILE_PATH). Paths are resolved relative to the configuration directory.

### Authorization (default-deny)
`CHAT_ID` is a single chat id or comma-separated whitelist stored in each instance's Settings.
It is **required**: initialization raises if it is empty, because an
empty value would let anyone who finds the bot read the ledger and commit with the owner's
`GITHUB_TOKEN`. All three entry points (`handle_message`, `handle_photo_message`,
`handle_callback_query`) gate on `is_authorized(chat_id)` and drop unauthorized traffic
silently — no reply, so the bot does not confirm its own existence to strangers.
`process_updates` authorizes before storing or scheduling messages. Logged text is scrubbed
with `_scrub()` to remove terminal control characters.

### Beancount storage (GitHub)
- `FILE_PATH` — journal being appended; `LEDGER_ROOT` selects the full ledger including it
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
Manual entries use `append_to_file`, with bounded stale-SHA retries. LLM entries use
`commit_llm_entry`: download immutable blobs from one tree, check the complete candidate
ledger locally, run an independent prompt/journal review, then check the tree again before
writing via the shared `_github_put_file`. Every conflict rebuilds and checks the candidate.
A stable `; telegram-operation: <uuid>` marker reconciles a PUT whose response was lost.
Exceptions and HTTP failures restore the draft with automatic confirmation disabled.
`/undo` rewrites precomputed content, so a conflict requires a fresh `/undo`.

### Conversational queries (NL → BQL)
Single-line text first goes through `route_intent()`, one temperature-0 LLM call that
classifies entry-vs-query and, for a query, emits the BQL in the same response
(`QUERY_ROUTER_SYSTEM_PROMPT`). `load_ledger()` mirrors a complete tree snapshot locally and
uses `ledger_validation.check_ledger`, which invokes the same loader and hardcore checks as
`bean-check`. Missing downloads, truncated trees and loader errors block queries; no partial
ledger fallback is used. Parsed entries and downloaded snapshots are cached by tree SHA.
`run_bql()` runs it; `answer_query()` wraps the same feed-error-back-to-LLM retry loop as
syntax validation (a download failure is not retried). Queries are read-only (no draft), and
`format_query_result()` sizes columns by `display_width()` (CJK=2) because beancount's own
renderer miscounts. Prompt gotcha: `units`/`cost` are functions, not columns.

### Payee context for drafts

When the router classifies input as an entry, two best-effort lookups enrich the draft prompt so the LLM matches the user's own conventions. `handle_message` loads the ledger and passes the result to both helpers via `loaded=`. A successful load is reused; `None` currently causes helpers to attempt loading again, so a failure can produce repeated diagnostics. Context lookup failures log and fall back to generating without that context; the mandatory pre-commit check remains.

- **Same-payee history** (`examples_for_payee`): if the router named a `payee`, the user's most recent past transactions (up to 10, loose case-folded substring match, either direction) are rendered back to beancount text via `_format_example_entry` (header + postings only, no metadata) and shown so the LLM reuses the account/narration/currency conventions.
- **Frequent-payee list** (`frequent_payees`): the top 50 payees by frequency across the whole ledger are handed to the LLM so it snaps a fuzzy input onto an existing merchant spelling instead of coining a near-duplicate. The ranked list is cached in `_ledger_cache["payees"]` keyed by tree sha (recomputed only when the ledger changes, guarded by an `entries is entries` identity check against a concurrent reload). Interior whitespace is collapsed so a multi-line payee stays a single `、`-joined token.

Both feed into `build_user_prompt(..., examples, payees)`, threaded through `call_openai_compatible`. The router's payee is only a retrieval hint, not a fixed field in the final entry. The generation call still receives the complete transaction input and selects the final payee itself. For example, the fictional hint `Demo Cafe` can retrieve `Demo Cafe 42`; the program does not append `42` itself. Substring retrieval is not typo matching: there is no edit-distance correction, and a short hint can retrieve several distinct merchants. A misspelling such as `sainsburry` will not normally match `Sainsbury's` directly. Either LLM stage may resolve the spelling using its input/context, but this is best-effort, not guaranteed. The frequent list is limited to 50 names and may omit a rare merchant. The history-injection log shows the retrieval hint, not the matched names or final payee.

### Beancount syntax validation
After `normalize_and_validate_llm_entry()`, every LLM-generated entry is validated with `beancount.parser.parser.parse_string()`. If the parser reports errors, the entry + error message are sent back to the LLM for correction, up to `MAX_BEANCOUNT_RETRIES` (3) retries. Both text (`call_openai_compatible`) and vision (`call_openai_vision_invest`) paths use this retry loop.

The production generation-stage ledger check is retained: cached snapshots provide early
semantic errors to the same retry loop; exhaustion uses `explain_ledger_error` for Chinese
advice. This early check attributes newly introduced errors, but it never replaces the
mandatory fresh, complete bean-check immediately before commit. A missing generation-time
snapshot cannot bypass the commit gate.

### Pending draft lifecycle
1. LLM generates entry → beancount syntax validated (with auto-retry) → stored in `Bot.pending_llm_entries` with `_make_pending_entry()`
2. Bot sends entry text + inline confirm/decline/edit buttons to user
3. On confirm or timeout → `approve_pending` → local bean-check + LLM consistency review → commit

Automatic confirmation is armed only after the review message is delivered. Feedback pauses
it; failed checks retain the draft for explicit retry without repeated automatic attempts.
Both approval paths atomically claim the draft. Screenshot file IDs and feedback are retained
for review. SQLite checkpoints preserve drafts, in-flight claims, IDs and feedback across
restart. A journal write uses its stable operation/update ID to reconcile replay. Interrupted
feedback stays paused. Undo still expires by canceling. Never delete production state on deploy.

### User customization

Only `user.md.example` is tracked; copy it to gitignored `user.md` for personal preferences. Never force-add the personal file or copy its contents into the template. Existing deployments must back up `user.md` outside the repository before pulling the untracking change and restore it afterward. Untracking does not erase Git history.

`_call_llm_backends` reloads root `user.md` for every logical request and includes it on every backend attempt, covering routing, generation, retries, vision and review. HTML comments are excluded. Missing/empty files add no prompt; the template is not a fallback. Preferences cannot bypass the task's output contract or mandatory validation.

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
- Fixed worker lanes preserve per-chat order, including timeout approval. Capacity is bounded.
- SQLite inbox acknowledgment follows durable enqueue; interrupted jobs replay on startup.
- `_ThreadHTTP` keeps one requests.Session per thread; sessions are never shared across workers.

### Input validation
- `/open` validates account name against beancount pattern (`^[A-Z][a-zA-Z0-9]*(?::[A-Z][a-zA-Z0-9]*)+$`) and currency against `^[A-Z][A-Z0-9]{0,9}$`
- `/update` validates amount is numeric
- Manual (hand-typed multi-line) transactions validate each currency against beancount's real
  commodity rule (`^[A-Z][A-Z0-9'._-]{0,22}[A-Z0-9]$` — 2–24 chars, upper-alpha start, alnum
  end), escape `\` and `"` in payee/narration, and run `validate_beancount_syntax` on the
  rendered directive **before** commit — so a bad hand entry can't poison the ledger and every
  downstream reader (`/last`, `/today`, `/undo`, NL→BQL). This mirrors the LLM path's validation.
- Telegram counts message length in UTF-16 code units, so every length cap uses `_utf16_len`
  (an astral emoji = 2). Plain text is capped in `send_message`; HTML code blocks (`/last`,
  `/today`, query results) go through `_capped_code_block`, which shrinks the raw text until the
  html-escaped, wrapped result fits — escaping alone could otherwise reflow it back over 4096.

### GitHub Actions workflows (`.github/workflows/*.yml.example`)
- `monthly-report.yml.example` — daily Sankey chart of monthly expenses sent to Telegram; configurable `REPORT_CURRENCY` and `FX_RATES` (JSON dict) at workflow `env` level; aggregates sub-accounts into top-level categories
- `notify-on-push.yml.example` — on push to main: sends expense breakdown + affected account balances to Telegram; reads account names from commit body
