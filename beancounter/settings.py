"""Explicit configuration loading; importing the bot never reads credentials."""

import json
import re
from pathlib import Path

from requests.structures import CaseInsensitiveDict
from requests.utils import check_header_validity


def llm_headers(backend):
    """Build fresh endpoint-local headers, with case-insensitive overrides/removals."""
    headers = CaseInsensitiveDict({
        "Authorization": f"Bearer {backend['api_key']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    for name, value in backend.get("headers", {}).items():
        if value is None:
            headers.pop(name, None)
        else:
            headers[name] = value
    return dict(headers)


def _validate_llm_headers(headers):
    if not isinstance(headers, dict):
        raise ValueError("LLM_HEADERS must be an object of header names to strings or null")
    for name, value in headers.items():
        if not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise ValueError("LLM_HEADERS contains an invalid header name")
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError("LLM_HEADERS values must be strings or null")
        try:
            check_header_validity((name, value))
            value.encode("latin-1")
            if any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in value):
                raise ValueError("control character")
        except (ValueError, UnicodeError):
            raise ValueError("LLM_HEADERS contains an invalid header value") from None
    return dict(headers)


class Settings:
    def __init__(self, values: dict, base_dir: Path | None = None):
        self.base_dir = Path(base_dir or Path(__file__).resolve().parent.parent).resolve()
        for key in ("GITHUB_TOKEN", "REPO_OWNER", "REPO_NAME", "BRANCH_NAME", "FILE_PATH",
                    "TIMEZONE", "TELEGRAM_BOT_TOKEN"):
            if not isinstance(values.get(key), str) or not values[key].strip():
                raise ValueError(f"Missing config: {key}")
            setattr(self, key, values[key])
        self.ALLOWED_CHATS = {s.strip() for s in str(values.get("CHAT_ID") or "").split(",") if s.strip()}
        if not self.ALLOWED_CHATS:
            raise ValueError("CHAT_ID is required; configure at least one authorized chat.")
        self.LEDGER_ROOT = values.get("LEDGER_ROOT")
        self.GITHUB_HEADERS = {"Authorization": f"token {self.GITHUB_TOKEN}",
                               "Accept": "application/vnd.github.object",
                               "X-GitHub-Api-Version": "2022-11-28"}
        self.LLM_BACKENDS = []
        self.LLM_REVIEW_ENABLED = values.get("LLM_REVIEW_ENABLED", True)
        if not isinstance(self.LLM_REVIEW_ENABLED, bool):
            raise ValueError("LLM_REVIEW_ENABLED must be true or false")
        for backend in values.get("LLM_BACKENDS") or []:
            url, key, model = (backend.get(k, "") for k in ("LLM_API_BASE_URL", "LLM_API_KEY", "LLM_MODEL"))
            if url and key and model:
                self.LLM_BACKENDS.append({"base_url": url.rstrip("/"), "api_key": key, "model": model,
                                          "vision_model": backend.get("LLM_VISION_MODEL") or model,
                                          "headers": _validate_llm_headers(backend.get("LLM_HEADERS", {}))})
        for name, default in (("ACCOUNTS_CACHE_TTL", 300), ("DRAFT_TTL_SECONDS", 120),
                              ("WORKERS", 4), ("QUEUE_SIZE", 64),
                              ("ANALYSIS_REQUEST_TIMEOUT_SECONDS", 180),
                              ("ANALYSIS_TIMEOUT_SECONDS", 600),
                              ("ANALYSIS_PROBE_BUDGET_SECONDS", 120)):
            try:
                number = int(values.get(name, default))
            except (TypeError, ValueError):
                number = default
            setattr(self, name, number if number > 0 else default)
        self.STATE_PATH = str(self.base_dir / values.get("STATE_PATH", "data/bot.sqlite3"))
        self.USER_PROMPT_PATH = self.base_dir / "user.md"

    @classmethod
    def load(cls, path: str | Path | None = None):
        path = Path(path) if path else Path(__file__).resolve().parent.parent / "config.json"
        with path.open(encoding="utf-8") as source:
            return cls(json.load(source), path.resolve().parent)

    @property
    def identity(self):
        return [self.TELEGRAM_BOT_TOKEN.split(":", 1)[0], self.REPO_OWNER, self.REPO_NAME,
                self.BRANCH_NAME, self.FILE_PATH, sorted(self.ALLOWED_CHATS)]
