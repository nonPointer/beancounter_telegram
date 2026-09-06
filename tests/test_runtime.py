"""Restart, delivery and bounded-concurrency contracts, with real SQLite."""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile
import threading
import time
import subprocess
import runpy
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from beancounter.bot import Bot
from beancounter.settings import Settings
from beancounter.dispatch import Dispatcher
from beancounter.state_store import StateStore
from beancounter.ledger_validation import check_ledger, load_ledger_texts
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
        with patch("beancounter.dispatch.traceback.print_exc"):
            dispatcher.submit(123, 1, lambda: 1 / 0)
            dispatcher.submit(123, 2, lambda: seen.append(2))
            dispatcher.close()
        self.assertEqual(seen, [2])


class TestExplicitSettings(unittest.TestCase):
    def test_import_does_not_load_configuration(self):
        result = subprocess.run([sys.executable, "-c",
            "from beancounter import settings; settings.Settings.load = lambda *a: (_ for _ in ()).throw(AssertionError('config read')); from beancounter import bot as main"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_interactive_preview_requires_opt_in_before_loading_config(self):
        with patch.object(sys, "argv", ["preview_llm.py"]), patch("sys.stderr"), \
             patch.object(Settings, "load") as load_config:
            with self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "preview_llm.py"), run_name="__main__")
        self.assertEqual(caught.exception.code, 2)
        load_config.assert_not_called()

    def test_package_move_preserves_default_personal_paths(self):
        root = Path(__file__).resolve().parents[1]
        with patch("beancounter.settings.Path.open"), \
             patch("beancounter.settings.json.load", return_value=MOCK_CONFIG):
            settings = Settings.load()
        self.assertEqual(settings.base_dir, root)
        self.assertEqual(settings.USER_PROMPT_PATH, root / "user.md")
        self.assertEqual(Path(settings.STATE_PATH), root / "data" / "bot.sqlite3")
        self.assertEqual(Settings(MOCK_CONFIG).base_dir, root)

    def test_entrypoint_and_templates_work_outside_repository(self):
        root = Path(__file__).resolve().parents[1]
        code = (
            "import sys, runpy; from unittest.mock import patch; "
            f"sys.path.insert(0, {str(root)!r}); "
            "import main; from beancounter.bot import Bot; "
            "from beancounter.bot_utils import jinja2; "
            "assert main.Bot is Bot; jinja2.get_template('transaction.bean.j2'); "
            "guard = patch('beancounter.bot.run'); mocked = guard.start(); "
            "runpy.run_path(main.__file__, run_name='__main__'); mocked.assert_called_once()"
        )
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-c", code], cwd=directory, capture_output=True, text=True)
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

    def test_draft_and_commit_checks_log_full_diagnostics(self):
        texts = {"main.bean": "2000-01-01 commodity O\n" * 10}
        for check in (load_ledger_texts, check_ledger):
            with self.subTest(check=check.__name__), patch("beancounter.ledger_validation.log") as logged:
                if check is check_ledger:
                    with self.assertRaises(ValueError):
                        check(texts, "main.bean")
                else:
                    check(texts, "main.bean")
                message = logged.call_args.args[0]
                self.assertEqual(message.count("Invalid token: 'O'"), 10)
                self.assertIn("main.bean:10:", message)
                self.assertNotIn("ledger_check_", message)


class TestDraftPublication(unittest.TestCase):
    def setUp(self):
        self.bot = Bot(settings=MOCK_CONFIG, state_path=":memory:")
        self.addCleanup(self.bot.close)
        self.entry = '2000-01-02 * "Example Shop"\n  Expenses:Food  1 GBP\n  Assets:Cash  -1 GBP'
        self.bot.send_draft_for_review = MagicMock()

    def publish(self, **kwargs):
        return self.bot.publish_llm_draft(123, self.entry, "entry", "original synthetic input",
                                          "2000-01-02", header="Synthetic draft:", **kwargs)

    def test_persisted_before_delivery_with_original_input_preserved(self):
        def delivered(chat, header, appendix, pid):
            saved = self.bot.state.get("drafts")["pending"][pid]
            self.assertEqual(saved["user_input"], "original synthetic input")
            self.assertFalse(saved["auto_confirm"])
            self.assertEqual(saved["appendix"], appendix)
            self.assertEqual(appendix.count('prompt: "processed synthetic input"'), 1)
            self.assertIn("Assets:Cash", saved["commit_message"])
        self.bot.send_draft_for_review.side_effect = delivered
        self.publish(prompt="processed synthetic input")
        self.bot.send_draft_for_review.assert_called_once()

    def test_captionless_photo_preserves_image_without_placeholder_metadata(self):
        pid = self.publish(prompt="", photo_file_id="synthetic-photo")
        pending = self.bot.pending_llm_entries[pid]
        self.assertEqual(pending["photo_file_id"], "synthetic-photo")
        self.assertNotIn("prompt:", pending["appendix"])

    def test_replacement_preserves_feedback_and_image_in_persisted_state(self):
        original_id = self.publish(photo_file_id="synthetic-photo")
        original = self.bot.pending_llm_entries[original_id]
        original["feedback"] = ["synthetic correction"]
        pid = self.publish(replaces=(original_id, original))
        saved = self.bot.state.get("drafts")["pending"]
        self.assertNotIn(original_id, saved)
        self.assertEqual(saved[pid]["feedback"], ["synthetic correction"])
        self.assertEqual(saved[pid]["photo_file_id"], "synthetic-photo")

    def test_discarded_original_is_not_resurrected(self):
        original_id = self.publish()
        original = self.bot.pending_llm_entries.pop(original_id)
        self.bot.send_draft_for_review.reset_mock()
        self.assertIsNone(self.publish(replaces=(original_id, original)))
        self.assertEqual(self.bot.pending_llm_entries, {})
        self.bot.send_draft_for_review.assert_not_called()


class TestPerformance(unittest.TestCase):
    def setUp(self):
        self.bot = Bot(settings=MOCK_CONFIG, state_path=":memory:")
        self.addCleanup(self.bot.close)
        self.paths = {"test.bean": "journal", "accounts/nested/assets.beancount": "accounts"}
        self.contents = {"journal": 'include "accounts/nested/assets.beancount"\n',
                         "accounts": '2000-01-01 open Assets:Cash GBP, USD, O.US ; sample wallet\n'}
        self.bot._list_bean_files = MagicMock(side_effect=lambda: ("tree", dict(self.paths)))
        self.bot._download_blob = MagicMock(side_effect=lambda sha: {"sha": sha, "content": self.contents[sha]})

    def test_only_changed_blobs_downloaded(self):
        self.bot._download_ledger_snapshot(("first", self.paths))
        self.contents["journal2"] = "; changed\n"
        second = {**self.paths, "test.bean": "journal2"}
        self.bot._download_blob.reset_mock()
        _, files = self.bot._download_ledger_snapshot(("second", second))
        self.bot._download_blob.assert_called_once_with("journal2")
        self.assertEqual(files["test.bean"]["content"], "; changed\n")

    def test_deleted_renamed_and_duplicate_blobs(self):
        self.bot._download_ledger_snapshot(("first", self.paths))
        self.bot._download_blob.reset_mock()
        paths = {"test.bean": "journal", "renamed.bean": "accounts", "copy.bean": "accounts"}
        _, files = self.bot._download_ledger_snapshot(("second", paths))
        self.bot._download_blob.assert_not_called()
        self.assertEqual(set(files), set(paths))

    def test_duplicate_blob_downloaded_once_on_cold_snapshot(self):
        self.bot._download_ledger_snapshot(("first", {"test.bean": "journal", "copy.bean": "journal"}))
        self.bot._download_blob.assert_called_once_with("journal")

    def test_failed_refresh_preserves_complete_snapshot(self):
        old = self.bot._download_ledger_snapshot(("first", self.paths))
        self.bot._download_blob.side_effect = ValueError("synthetic failure")
        with self.assertRaises(ValueError):
            self.bot._download_ledger_snapshot(("second", {**self.paths, "test.bean": "new"}))
        self.assertIs(self.bot._snapshot_cache, old)

    def test_accounts_include_all_currencies_and_share_blobs(self):
        self.assertEqual(self.bot.parse_accounts(), ["Assets:Cash"])
        self.assertEqual(self.bot._accounts_for_prompt(), ["Assets:Cash (GBP, USD, O.US) ; sample wallet"])
        self.bot._download_blob.reset_mock()
        self.bot._download_ledger_snapshot()
        self.bot._download_blob.assert_called_once_with("journal")

    def test_snapshot_blobs_reused_by_account_refresh(self):
        self.bot._download_ledger_snapshot()
        self.bot._download_blob.reset_mock()
        self.assertEqual(self.bot.parse_accounts(), ["Assets:Cash"])
        self.bot._download_blob.assert_not_called()

    def test_invalid_accounts_do_not_replace_last_complete_cache(self):
        previous = self.bot.parse_accounts()
        self.paths["accounts/nested/assets.beancount"] = "invalid"
        self.contents["invalid"] = "2000-01-01 open Assets:Cash O\n"
        self.bot._accounts_cache["ts"] = 0
        self.assertIs(self.bot.parse_accounts(), previous)
        self.assertEqual(self.bot._accounts_cache["ts"], 0)

    def test_explicit_failed_context_never_reloads(self):
        self.bot.load_ledger = MagicMock(side_effect=AssertionError("must not reload"))
        self.assertIsNone(self.bot.examples_for_payee("Example", loaded=None))
        self.assertEqual(self.bot.frequent_payees(loaded=None), [])
        self.assertIsNone(self.bot._draft_ledger_context(None))
        self.bot.load_ledger.assert_not_called()

    def test_unsupplied_context_still_loads(self):
        self.bot.load_ledger = MagicMock(return_value=None)
        self.bot.examples_for_payee("Example")
        self.bot.frequent_payees()
        self.bot._draft_ledger_context()
        self.assertEqual(self.bot.load_ledger.call_count, 3)

    def test_generator_does_not_retry_explicit_failed_context(self):
        self.bot.llm_enabled = True
        self.bot.load_ledger = MagicMock(side_effect=AssertionError("must not reload"))
        self.bot._call_llm_backends = MagicMock(return_value='2000-01-02 * "Example"\n  Expenses:Food  1 GBP\n  Assets:Cash  -1 GBP')
        self.bot.validate_entry_against_ledger = MagicMock(side_effect=AssertionError("no context"))
        entry = self.bot.call_openai_compatible("synthetic input", ["Assets:Cash", "Expenses:Food"], "2000-01-02", loaded=None)
        self.assertIn("Expenses:Food", entry)
        self.bot.load_ledger.assert_not_called()
        self.bot.validate_entry_against_ledger.assert_not_called()

    def test_account_tree_download_uses_verified_blob_responses(self):
        import base64
        del self.bot._list_bean_files
        del self.bot._download_blob
        def response(url, **kwargs):
            result = MagicMock(status_code=200)
            if "/git/trees/" in url:
                result.json.return_value = {"sha": "tree", "tree": [
                    {"path": p, "sha": sha, "type": "blob"} for p, sha in self.paths.items()]}
            else:
                sha = url.rsplit("/", 1)[1]
                result.json.return_value = {"sha": sha, "encoding": "base64",
                    "content": base64.b64encode(self.contents[sha].encode()).decode()}
            return result
        with patch("beancounter.ledger.HTTP.get", side_effect=response) as get:
            self.assertEqual(self.bot.parse_accounts(), ["Assets:Cash"])
            self.assertEqual(get.call_count, 2)
        self.assertEqual(self.bot._accounts_cache["currencies"]["Assets:Cash"], "GBP, USD, O.US")

    def test_concurrent_snapshot_downloads_reuse_single_refresh(self):
        from concurrent.futures import ThreadPoolExecutor
        gate = threading.Barrier(2)
        def load():
            gate.wait(timeout=5)
            return self.bot._download_ledger_snapshot(("tree", self.paths))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(load), pool.submit(load)
            self.assertIs(first.result(5), second.result(5))
        self.assertEqual(self.bot._download_blob.call_count, 2)

    def test_account_refreshes_are_coalesced_and_closed_accounts_excluded(self):
        from concurrent.futures import ThreadPoolExecutor
        self.contents["accounts"] += "2000-01-01 open Assets:Old\n2000-02-01 close Assets:Old\n"
        gate = threading.Barrier(2)
        def load():
            gate.wait(timeout=5)
            return self.bot.parse_accounts()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(load), pool.submit(load)
            self.assertEqual(first.result(5), ["Assets:Cash"])
            self.assertEqual(second.result(5), ["Assets:Cash"])
        self.bot._list_bean_files.assert_called_once()
        self.bot._download_blob.assert_called_once_with("accounts")

    def test_download_pool_is_closed_with_bot(self):
        self.bot._download_ledger_snapshot()
        self.bot.close()
        with self.assertRaises(RuntimeError):
            self.bot._downloads.submit(lambda: None)

    def test_concurrent_context_loads_parse_once(self):
        from concurrent.futures import ThreadPoolExecutor
        entered, release = threading.Event(), threading.Event()
        result = ([], {})
        def checked(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError("release timeout")
            return result
        with patch("beancounter.ledger.check_ledger", side_effect=checked) as check, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.bot.load_ledger)
            self.assertTrue(entered.wait(5))
            second = pool.submit(self.bot.load_ledger)
            release.set()
            self.assertEqual(first.result(5), result)
            self.assertEqual(second.result(5), result)
        check.assert_called_once()
        self.assertEqual(self.bot._download_blob.call_count, 2)

    def test_full_lane_does_not_block_other_lanes_or_reorder_its_own(self):
        self.bot.state.queued = MagicMock(return_value=[
            (1, 4, {"message": {"text": "first"}}),
            (2, 8, {"message": {"text": "same lane"}}),
            (3, 5, {"message": {"text": "other lane"}}),
        ])
        self.bot._spawn_handler = MagicMock(side_effect=[False, True])
        self.bot._schedule_inbox()
        self.assertEqual([c.args[2] for c in self.bot._spawn_handler.call_args_list], [4, 5])

    def test_stage_timings_logged_even_on_failure(self):
        from beancounter.bot_utils import timed
        with patch("beancounter.bot_utils.log") as logged:
            with self.assertRaises(ValueError), timed("synthetic stage"):
                raise ValueError("synthetic failure")
        self.assertIn("Timing [synthetic stage]", logged.call_args.args[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
