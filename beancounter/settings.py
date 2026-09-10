"""Explicit configuration loading; importing the bot never reads credentials."""

import json
from pathlib import Path


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
        for backend in values.get("LLM_BACKENDS") or []:
            url, key, model = (backend.get(k, "") for k in ("LLM_API_BASE_URL", "LLM_API_KEY", "LLM_MODEL"))
            if url and key and model:
                self.LLM_BACKENDS.append({"base_url": url.rstrip("/"), "api_key": key, "model": model,
                                          "vision_model": backend.get("LLM_VISION_MODEL") or model})
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
