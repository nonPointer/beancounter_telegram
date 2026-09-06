"""Durable draft checkpoints and a bounded inbox, using only the standard library."""

import json
import sqlite3
import threading
import fcntl
from pathlib import Path


class StateStore:
    def __init__(self, path: str, identity):
        self.process_lock = None
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.process_lock = open(path + ".lock", "a")
            try:
                fcntl.flock(self.process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.process_lock.close()
                raise ValueError("Another bot is using this STATE_PATH; stop it before starting a second instance.")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox (
                id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued');
        """)
        stored = self.get("identity")
        if stored is not None and stored != identity:
            self.db.close()
            if self.process_lock:
                self.process_lock.close()
            raise ValueError("State database belongs to a different bot/repository. Use a different STATE_PATH.")
        self.set("identity", identity)
        # Jobs are replayable; ledger writes use stable update/operation identifiers.
        with self.db:
            self.db.execute("UPDATE inbox SET status='queued' WHERE status='processing'")

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, json.dumps(value)))

    def enqueue(self, updates: list[tuple[int, int, dict]], offset: int, capacity: int) -> bool:
        with self.lock, self.db:
            count = self.db.execute("SELECT count(*) FROM inbox").fetchone()[0]
            old_offset = self.get("offset", 0)
            fresh = [u for u in updates if u[0] > old_offset]
            if count + len(fresh) > capacity:
                return False
            self.db.executemany("INSERT OR IGNORE INTO inbox(id, chat_id, payload) VALUES (?, ?, ?)",
                                [(uid, chat, json.dumps(payload)) for uid, chat, payload in fresh])
            self.db.execute("INSERT OR REPLACE INTO state VALUES ('offset', ?)", (str(max(offset, old_offset)),))
            return True

    def queued(self):
        with self.lock:
            return [(uid, chat, json.loads(payload)) for uid, chat, payload in
                    self.db.execute("SELECT id, chat_id, payload FROM inbox WHERE status='queued' ORDER BY id")]

    def begin(self, uid):
        with self.lock, self.db:
            self.db.execute("UPDATE inbox SET status='processing' WHERE id=?", (uid,))

    def finish(self, uid):
        with self.lock, self.db:
            self.db.execute("DELETE FROM inbox WHERE id=?", (uid,))

    def close(self):
        with self.lock:
            self.db.close()
            if self.process_lock:
                self.process_lock.close()
