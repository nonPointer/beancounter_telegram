"""Restart, delivery and bounded-concurrency contracts, with real SQLite."""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile
import threading
import time
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from main import Bot
from settings import Settings
from dispatch import Dispatcher
from state_store import StateStore
from ledger_validation import check_ledger
from test_bot import MOCK_CONFIG, FakeGitHub


class TestRuntime(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name, "state.sqlite3"))

    def bot(self, **changes):
        bot = Bot(settings={**MOCK_CONFIG, **changes}, state_path=self.path)
        bot.send_message = MagicMock(return_value={"ok": True, "result": {"message_id": 7}})
        bot.edit_message_reply_markup = MagicMock()
        self.addCleanup(bot.close)
        return bot

    def test_pending_feedback_photo_timezone_and_counter_survive_restart(self):
        first = self.bot()
        pid = first.next_pending_id()
        pending = first._make_pending_entry(123, "journal", "commit", "原始输入", "2026-01-01")
        pending.update(photo_file_id="photo", feedback=["修正"], rechecking=True)
        first.pending_llm_entries[pid] = pending
        first.pending_decline_reasons[123] = pid
        first.close()
        second = self.bot()
        restored = second.pending_llm_entries[pid]
        self.assertEqual(restored["photo_file_id"], "photo")
        self.assertEqual(restored["feedback"], ["修正"])
        self.assertFalse(restored["auto_confirm"])
        self.assertEqual(second.pending_decline_reasons[123], pid)
        self.assertGreater(int(second.next_pending_id()), int(pid))

    def test_confirm_inflight_at_restart_reconciles_a_completed_put(self):
        first = self.bot()
        pid = first.next_pending_id()
        pending = first._make_pending_entry(123, "journal", "commit", "input", "2026-01-01")
        pending.update(auto_confirm=True, created_at=0)
        first.pending_llm_entries[pid] = pending
        with first._pending_lock:
            first._claim_pending_locked(pid)
        first.close()  # Simulate a process ending after remote success but before local completion.
        second = self.bot()
        content = f'; telegram-operation: {pending["operation_id"]}\njournal\n'
        second._download_ledger_snapshot = lambda: ("tree", {"test.bean": {"content": content, "sha": "sha"}})
        second._github_put_file = MagicMock()
        second._call_llm_backends = MagicMock()
        second._handle_expired_draft({"_draft_id": pid})
        second._github_put_file.assert_not_called()
        second._call_llm_backends.assert_not_called()
        self.assertNotIn(pid, second.pending_llm_entries)
        self.assertEqual(second.state.get("drafts")["inflight"], {})

    def test_only_one_instance_can_open_the_state_database(self):
        first = self.bot()
        with self.assertRaisesRegex(ValueError, "Another bot"):
            self.bot()
        first.close()
        self.bot()

    def test_state_cannot_be_reused_for_a_different_repository(self):
        self.bot().close()
        with self.assertRaisesRegex(ValueError, "different bot/repository"):
            self.bot(REPO_NAME="another-ledger")

    def test_durable_inbox_replays_in_order_after_restart(self):
        first = self.bot(WORKERS=2, QUEUE_SIZE=4)
        updates = [(uid, 123, {"update_id": uid, "message": {"chat": {"id": 123}, "text": str(uid)}})
                   for uid in (1, 2, 3)]
        self.assertTrue(first.state.enqueue(updates, 3, 8))
        first.state.begin(1)
        first.close()
        second = self.bot(WORKERS=2, QUEUE_SIZE=4)
        seen = []
        second.handle_message = lambda update: seen.append(update["update_id"])
        second._schedule_inbox()
        second.dispatcher.close()
        self.assertEqual(seen, [1, 2, 3])
        self.assertEqual(second.update_id, 3)
        self.assertEqual(second.state.queued(), [])

    def test_full_inbox_does_not_ack_a_new_batch(self):
        bot = self.bot()
        self.assertTrue(bot.state.enqueue([(1, 123, {"update_id": 1})], 1, 1))
        self.assertFalse(bot.state.enqueue([(2, 123, {"update_id": 2})], 2, 1))
        self.assertEqual(bot.state.get("offset"), 1)
        self.assertEqual(len(bot.state.queued()), 1)

    def test_unauthorized_message_never_reaches_a_worker(self):
        bot = self.bot()
        fn = MagicMock()
        self.assertFalse(bot._spawn_handler(fn, {}, 999))
        self.assertEqual(bot.dispatcher.threads, [])
        fn.assert_not_called()

    def test_replayed_manual_write_is_idempotent(self):
        github = FakeGitHub()
        for _ in range(2):
            bot = self.bot()
            bot.github_download_file = github.download
            bot._github_put_file = github.put
            bot._handler_context.update_id = 1234
            self.assertTrue(bot.append_to_file("journal", "commit")[0])
            bot.close()
        self.assertEqual(len(github.commits), 1)

    def test_deadline_task_waits_for_earlier_feedback(self):
        bot = self.bot(WORKERS=1)
        pending = bot._make_pending_entry(123, "journal", "commit", "input", "2026-01-01")
        pending.update(created_at=0, auto_confirm=True)
        bot.pending_llm_entries["1"] = pending
        bot.commit_llm_entry = MagicMock()
        bot._spawn_handler(lambda _: pending.update(auto_confirm=False), {}, 123)
        bot.cleanup_expired_drafts()
        bot.dispatcher.close()
        bot.commit_llm_entry.assert_not_called()
        self.assertIn("1", bot.pending_llm_entries)


class TestDispatcher(unittest.TestCase):
    def test_fifo_capacity_and_fixed_worker_count(self):
        dispatcher = Dispatcher(1, 1)
        gate, started = threading.Event(), threading.Event()
        seen = []
        def first():
            started.set()
            gate.wait(2)
            seen.append(1)
        try:
            self.assertTrue(dispatcher.submit(123, 1, first))
            self.assertTrue(started.wait(1))
            self.assertTrue(dispatcher.submit(123, 2, lambda: seen.append(2)))
            self.assertFalse(dispatcher.submit(123, 3, lambda: seen.append(3)))
            self.assertEqual(len(dispatcher.threads), 1)
        finally:
            gate.set()
            dispatcher.close()
        self.assertEqual(seen, [1, 2])

    def test_one_failed_job_does_not_kill_a_worker(self):
        dispatcher = Dispatcher(1, 4)
        seen = []
        with patch("dispatch.traceback.print_exc"):
            dispatcher.submit(123, 1, lambda: 1 / 0)
            dispatcher.submit(123, 2, lambda: seen.append(2))
            dispatcher.close()
        self.assertEqual(seen, [2])


class TestExplicitSettings(unittest.TestCase):
    def test_import_does_not_load_configuration(self):
        result = subprocess.run([sys.executable, "-c",
            "import settings; settings.Settings.load = lambda *a: (_ for _ in ()).throw(AssertionError('config read')); import main"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_instances_do_not_share_credentials_or_whitelists(self):
        first = Bot(settings=MOCK_CONFIG, state_path=":memory:")
        second = Bot(settings={**MOCK_CONFIG, "CHAT_ID": "999", "GITHUB_TOKEN": "other"}, state_path=":memory:")
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.assertTrue(first.is_authorized(123))
        self.assertFalse(second.is_authorized(123))
        self.assertNotEqual(first.settings.GITHUB_HEADERS, second.settings.GITHUB_HEADERS)

    def test_invalid_tuning_uses_defaults(self):
        settings = Settings({**MOCK_CONFIG, "WORKERS": -1, "QUEUE_SIZE": "invalid"})
        self.assertEqual(settings.WORKERS, 4)
        self.assertEqual(settings.QUEUE_SIZE, 64)

    def test_root_must_actually_include_the_journal(self):
        texts = {"main.bean": "; empty root\n", "test.bean": "; journal\n"}
        with self.assertRaisesRegex(ValueError, "does not include"):
            check_ledger(texts, "main.bean", "test.bean")
        texts["main.bean"] = 'include "test.bean"\n'
        check_ledger(texts, "main.bean", "test.bean")

    def test_ledger_errors_include_all_locations_without_temporary_paths(self):
        texts = {"main.bean": 'include "nested/bad.bean"\n',
                 "nested/bad.bean": "2000-01-01 commodity O\n" * 10}
        with self.assertRaises(ValueError) as caught:
            check_ledger(texts, "main.bean")
        diagnostic = str(caught.exception)
        self.assertIn("bean-check failed (10 errors)", diagnostic)
        self.assertEqual(diagnostic.count("Invalid token: 'O'"), 10)
        for line in range(1, 11):
            self.assertIn(f"nested/bad.bean:{line}:", diagnostic)
        self.assertNotIn("ledger_check_", diagnostic)

    def test_ledger_errors_include_related_entry(self):
        texts = {"main.bean": '2000-01-01 * "Unknown account"\n  Assets:Missing  1 USD\n  Equity:Missing  -1 USD\n'}
        with self.assertRaises(ValueError) as caught:
            check_ledger(texts, "main.bean")
        self.assertIn("main.bean:1:", str(caught.exception))
        self.assertIn('2000-01-01 * "Unknown account"', str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
