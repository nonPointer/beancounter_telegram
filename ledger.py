"""Ledger responsibilities of Bot; shared entry points are preserved."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from beancount.core.data import Transaction
from concurrent.futures import as_completed
import base64
from beancount.query import query as beancount_query
from ledger_validation import check_ledger, load_ledger_texts
from pathlib import Path
import re
import time
from bot_utils import (
    AccountMatchError, GITHUB_CONFLICT_RETRIES, GITHUB_URL_BASE, HTTP, log,
)

class LedgerMixin:
    def parse_accounts(self):
        now = time.time()
        with self._accounts_cache_lock:
            if self._accounts_cache["accounts"] is not None and now - self._accounts_cache["ts"] < self.settings.ACCOUNTS_CACHE_TTL:
                return self._accounts_cache["accounts"]

        list_headers = {
            "Authorization": f"token {self.settings.GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        url = f"{GITHUB_URL_BASE}/repos/{self.settings.REPO_OWNER}/{self.settings.REPO_NAME}/contents/accounts?ref={self.settings.BRANCH_NAME}"
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
            if not fetch_ok:
                # Do not replace a complete cache with partial results or refresh its TTL.
                return self._accounts_cache["accounts"] or []
            self._accounts_cache["accounts"] = accounts
            self._accounts_cache["currencies"] = currencies
            self._accounts_cache["comments"] = comments
            self._accounts_cache["sha_map"] = new_map
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
        exact = [a for a in accounts if a.lower() == suffix_lower]
        if exact:
            return exact[0]
        matches = [a for a in accounts if a.lower().endswith(suffix_lower)]
        if len(matches) > 1:
            raise AccountMatchError("账户简称有歧义，请使用完整账户名：\n" + "\n".join(matches))
        if not matches:
            log(f"No matching account for suffix: {account_suffix}")
            log(f"Available accounts: {accounts}")
        return matches[0] if matches else None

    def _list_bean_files(self) -> tuple[str, dict[str, str]] | None:
        """Return a tree SHA and path→blob SHA map for immutable ledger downloads."""
        url = f"{GITHUB_URL_BASE}/repos/{self.settings.REPO_OWNER}/{self.settings.REPO_NAME}/git/trees/{self.settings.BRANCH_NAME}?recursive=1"
        headers = {"Authorization": f"token {self.settings.GITHUB_TOKEN}",
                   "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        r = HTTP.get(url, headers=headers, timeout=30)
        if r.status_code != 200:
            log(f"Could not list repo tree: HTTP {r.status_code}")
            return None
        data = r.json()
        if data.get("truncated"):
            raise ValueError("Repo tree is truncated; cannot verify the complete ledger.")
        paths = {t["path"]: t["sha"] for t in data.get("tree", [])
                 if t.get("type") == "blob" and t["path"].endswith((".bean", ".beancount"))}
        return data["sha"], paths

    def _download_blob(self, sha: str) -> dict:
        url = f"{GITHUB_URL_BASE}/repos/{self.settings.REPO_OWNER}/{self.settings.REPO_NAME}/git/blobs/{sha}"
        response = HTTP.get(url, headers=self.settings.GITHUB_HEADERS, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data.get("encoding") != "base64" or data.get("sha") != sha:
            raise ValueError("Invalid GitHub blob response")
        return {"content": base64.b64decode(data["content"]).decode("utf-8"), "sha": sha}

    def _download_ledger_snapshot(self, listed=None) -> tuple[str, dict[str, dict]]:
        listed = listed if listed is not None else self._list_bean_files()
        if listed is None:
            raise ValueError("无法获取完整账本目录，请稍后重试。")
        tree_sha, paths = listed
        with self._ledger_cache_lock:
            if self._snapshot_cache[0] == tree_sha:
                return self._snapshot_cache
        if self.settings.FILE_PATH not in paths:
            raise ValueError(f"Ledger root missing: {self.settings.FILE_PATH}")
        with ThreadPoolExecutor(max_workers=min(8, len(paths))) as pool:
            futures = {pool.submit(self._download_blob, sha): path for path, sha in paths.items()}
            files = {futures[future]: future.result() for future in as_completed(futures)}
        with self._ledger_cache_lock:
            self._snapshot_cache = (tree_sha, files)
        return tree_sha, files

    def load_ledger(self) -> tuple[list, dict] | None:
        """Check a complete immutable snapshot; never answer from a partial ledger."""
        listed = self._list_bean_files()
        if listed is None:
            raise ValueError("无法获取完整账本目录，查询已停止。")
        tree_sha, paths = listed

        if tree_sha is not None:
            with self._ledger_cache_lock:
                if self._ledger_cache["tree_sha"] == tree_sha:
                    return self._ledger_cache["entries"], self._ledger_cache["options_map"]

        _, files = self._download_ledger_snapshot(listed)
        texts = {path: f["content"] for path, f in files.items()}

        if not texts.get(self.settings.FILE_PATH):
            log("Ledger main file is empty or missing; cannot query.")
            return None

        entries, options_map = check_ledger(texts, self._ledger_root(texts), self.settings.FILE_PATH)
        with self._ledger_cache_lock:
            self._ledger_cache = {"tree_sha": tree_sha, "entries": entries,
                                  "options_map": options_map, "texts": texts}
        return entries, options_map

    def validate_entry_against_ledger(self, entry_text: str, loaded=None) -> str | None:
        """Early draft feedback; the mandatory fresh check still runs before every commit."""
        if loaded is None:
            try:
                loaded = self.load_ledger()
            except Exception as exc:
                log(f"Draft bean-check skipped: ledger unavailable ({type(exc).__name__}: {exc}).")
                return None
        if loaded is None:
            return None
        with self._ledger_cache_lock:
            texts = self._ledger_cache.get("texts")
            if texts is None or self._ledger_cache.get("entries") is not loaded[0]:
                return None
            candidate = dict(texts)
            budget = Counter(e.message for e in self._ledger_cache.get("errors") or [])
        base = candidate[self.settings.FILE_PATH].rstrip("\n")
        start_line = base.count("\n") + 3 if base else 1
        candidate[self.settings.FILE_PATH] = (base + "\n\n" if base else "") + entry_text + "\n"
        try:
            _, errors, _ = self._load_ledger_texts(candidate, self._ledger_root(candidate))
        except Exception as exc:
            log(f"Draft bean-check could not run ({type(exc).__name__}: {exc}); commit validation remains mandatory.")
            return None  # Generation can continue; the pre-commit gate cannot be skipped.
        new_errors = []
        for error in errors:
            source = error.source or {}
            in_new = (Path(source.get("filename") or "").name == Path(self.settings.FILE_PATH).name
                      and (source.get("lineno") or 0) >= start_line)
            if not in_new and budget[error.message] > 0:
                budget[error.message] -= 1
            else:
                new_errors.append(error.message)
        return "; ".join(dict.fromkeys(new_errors)) or None

    @staticmethod
    def _load_ledger_texts(texts: dict, root: str = "main.bean"):
        return load_ledger_texts(texts, root)

    def _ledger_root(self, texts):
        return self.settings.LEDGER_ROOT or ("main.bean" if "main.bean" in texts else self.settings.FILE_PATH)

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

    def github_download_file(self, file_path: str | None = None) -> dict | None:
        file_path = file_path or self.settings.FILE_PATH
        url = f"{GITHUB_URL_BASE}/repos/{self.settings.REPO_OWNER}/{self.settings.REPO_NAME}/contents/{file_path}?ref={self.settings.BRANCH_NAME}"
        headers = dict(self.settings.GITHUB_HEADERS)
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
                         file_path: str | None = None) -> tuple[bool, int]:
        """PUT a file and report the HTTP status, so callers can tell a stale-sha
        conflict (retryable) from a real failure (not)."""
        file_path = file_path or self.settings.FILE_PATH
        url = f"{GITHUB_URL_BASE}/repos/{self.settings.REPO_OWNER}/{self.settings.REPO_NAME}/contents/{file_path}"
        data = {
            "message": commit_message,
            "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
            "branch": self.settings.BRANCH_NAME,
        }
        if sha:
            data["sha"] = sha
        r = HTTP.put(url=url, headers=self.settings.GITHUB_HEADERS, json=data, timeout=30)
        if r.status_code in [200, 201]:
            self._file_etag_cache.pop(file_path, None)
            return True, r.status_code
        log(f"Error uploading file: {r.status_code}")
        log(r.text)
        return False, r.status_code

    def github_upload_file(self, content: str, sha: str, commit_message: str, file_path: str | None = None) -> bool:
        file_path = file_path or self.settings.FILE_PATH
        ok, _ = self._github_put_file(content, sha, commit_message, file_path)
        return ok

    def append_to_file(self, appendix: str, commit_message: str, file_path: str | None = None,
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
        file_path = file_path or self.settings.FILE_PATH
        update_id = getattr(self._handler_context, "update_id", None)
        marker = f"; telegram-update: {update_id}" if update_id is not None else None
        f = downloaded
        for attempt in range(1, GITHUB_CONFLICT_RETRIES + 1):
            if f is None:
                f = self.github_download_file(file_path)
                if not f:
                    return False, "Failed to download from GitHub."

            if marker and marker in f["content"].splitlines():
                return True, ""
            addition = (marker + "\n" if marker else "") + appendix
            ok, status = self._github_put_file(
                f["content"] + '\n' + addition + '\n', f["sha"], commit_message, file_path)
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
        url = f"{GITHUB_URL_BASE}/repos/{self.settings.REPO_OWNER}/{self.settings.REPO_NAME}/actions/workflows/{workflow_file}/dispatches"
        data = {"ref": self.settings.BRANCH_NAME, "inputs": inputs}
        r = HTTP.post(url=url, headers=self.settings.GITHUB_HEADERS, json=data, timeout=30)
        if r.status_code == 204:
            return True, ""
        else:
            error = f"{r.status_code} {r.text}"
            log(f"Error triggering workflow: {error}")
            return False, error
