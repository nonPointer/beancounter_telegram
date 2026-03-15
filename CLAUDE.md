# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the bot
.venv/bin/python main.py

# Run in debug mode (extra logging)
.venv/bin/python main.py debug

# Run tests (no pytest — use unittest directly)
.venv/bin/python test_refactor.py

# Interactive LLM test tool
.venv/bin/python test_llm.py

# Worker (Cloudflare) — from worker/ directory
npm run dev    # local dev
npm run deploy # deploy to Cloudflare
```

## Architecture

The project has two independent components that share the same config schema:

**1. Python Telegram Bot (`main.py`)** — the primary backend
- Polls Telegram for updates; spawns a daemon thread per message
- `Bot` class holds all state: pending drafts, account cache, LLM config
- Per-message flow: text → LLM → pending draft → user confirm/decline → GitHub commit
- Photo messages (investment screenshots) go through a vision LLM path (`call_openai_vision_invest`)

**2. Cloudflare Worker (`worker/src/index.ts`)** — webhook-based alternative
- TypeScript, same feature set, uses KV for pending entry state
- Configured via Cloudflare environment bindings (same key names as `config.json`)

### Key modules
- `main.py` — bot entry point, `Bot` class (~1430 lines)
- `prompts.py` — LLM system prompts and user prompt builders for both text and vision paths
- `templates/*.bean.j2` — Jinja2 templates for beancount directives (`open`, `close`, `balance`, `pad`, `transaction`)

### Config (`config.json`, gitignored)
Required keys: `GITHUB_TOKEN`, `REPO_OWNER`, `REPO_NAME`, `BRANCH_NAME`, `FILE_PATH`, `TIMEZONE`, `TELEGRAM_BOT_TOKEN`

LLM backends (array preferred, single-backend keys for backward compat):
```json
"LLM_BACKENDS": [{"LLM_API_BASE_URL": "...", "LLM_API_KEY": "...", "LLM_MODEL": "..."}]
```

Optional tuning: `ACCOUNTS_CACHE_TTL` (default 300s), `DRAFT_TTL_SECONDS` (default 120s), `CHAT_ID` (restrict to one chat)

### Beancount storage (GitHub)
- `main.bean` — top-level file (value of `FILE_PATH`)
- `accounts/{assets,liabilities,equity,income,expenses}.bean` — account definitions; fetched in parallel via `ThreadPoolExecutor` and cached for `ACCOUNTS_CACHE_TTL` seconds
- `ACCOUNT_TYPE_MAP` module constant maps lowercase prefix → bean file path

### LLM backend fallback
`LLM_BACKENDS` list is tried in order; `Bot._call_llm_backends(payload)` raises `ValueError` if all fail. Text and vision paths both use this helper. LLM output starting with `NEED_ACCOUNT:` signals missing account context and is surfaced as a user-facing error.

### Pending draft lifecycle
1. LLM generates entry → stored in `Bot.pending_llm_entries` with `_make_pending_entry()`
2. Bot sends entry text + inline confirm/decline/edit buttons to user
3. On confirm → GitHub commit; on decline → discard; on no action → expires after `DRAFT_TTL_SECONDS` and `cleanup_expired_drafts()` notifies user

Undo entries also use `pending_llm_entries` with `"kind": "undo"` to distinguish them from LLM draft entries. They store `new_content` and `file_sha` pre-computed at show-time.

### /undo command
- `/undo` — previews and removes the last beancount directive from `main.bean` (any top-level directive: transaction, balance, pad, open, close)
- `extract_last_directive_block(content)` — module-level pure function; scans backward for last `YYYY-MM-DD ` line, extracts the full block, returns `(directive_text, new_file_content)`
- Callback actions: `undo_confirm:<id>` commits `new_content` to GitHub; `undo_cancel:<id>` discards

### /last and /today commands
- `/last [N]` — shows the last N directives from `main.bean` (default 5); accepts optional count argument
- `/today` — shows all directives matching today's date (timezone-aware via `self.timezone`)
- Both use `extract_all_directive_blocks(content)` — module-level pure function that returns `[(date_str, block_text), ...]` in file order; each block includes leading `;` comment lines
- Read-only commands; no pending entry or confirmation flow

### GitHub file ETag caching
- `Bot._file_etag_cache` — dict keyed by file path, stores `{"etag", "content", "sha"}`
- `github_download_file()` sends `If-None-Match` header when cache exists; on `304 Not Modified`, returns cached content without re-downloading
- `github_upload_file()` invalidates the cache entry on success to ensure next read fetches fresh data

### Date handling
First line of user input is tested with `datetime.strptime(line, '%Y-%m-%d')`; if it parses, it overrides today's date and is stripped from the input before LLM call.
