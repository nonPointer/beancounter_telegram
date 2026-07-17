"""Unit tests for main.py Bot logic."""

# Run from anywhere: put the repo root on the path so `import main` resolves.
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

MOCK_CONFIG = {
    "GITHUB_TOKEN": "test_token",
    "REPO_OWNER": "test_owner",
    "REPO_NAME": "test_repo",
    "BRANCH_NAME": "main",
    "FILE_PATH": "test.bean",
    "TELEGRAM_BOT_TOKEN": "123:test",
    "TIMEZONE": "UTC",
    "CHAT_ID": "123",
    "LLM_BACKENDS": [],
}

# Patch config loading before importing main
with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(MOCK_CONFIG))):
    with patch("json.load", return_value=MOCK_CONFIG):
        import main
        from main import Bot


def make_bot() -> Bot:
    with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(MOCK_CONFIG))):
        with patch("json.load", return_value=MOCK_CONFIG):
            return Bot()


class TestStripCodeFence(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()

    def test_no_fence(self):
        self.assertEqual(self.bot.strip_code_fence("hello"), "hello")

    def test_with_fence(self):
        text = "```beancount\n2024-01-01 * \"Foo\"\n  Assets:Cash  10 USD\n```"
        result = self.bot.strip_code_fence(text)
        self.assertNotIn("```", result)
        self.assertIn("Assets:Cash", result)

    def test_fence_too_short(self):
        text = "```only one line```"
        self.assertEqual(self.bot.strip_code_fence(text), text.strip())


class TestEnsureDatetimeMetadata(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()

    def test_inserts_datetime(self):
        entry = '2024-01-01 * "Payee" "Narr"\n  Assets:Cash  10 USD\n  Expenses:Food  -10 USD'
        result = self.bot.ensure_datetime_metadata(entry, "2024-01-01 10:00:00")
        self.assertIn('datetime: "2024-01-01 10:00:00"', result)

    def test_no_duplicate_datetime(self):
        entry = '2024-01-01 * "Payee" "Narr"\n  datetime: "2024-01-01 10:00:00"\n  Assets:Cash  10 USD'
        result = self.bot.ensure_datetime_metadata(entry, "2024-01-01 10:00:00")
        self.assertEqual(result.count('datetime:'), 1)

    def test_inserts_after_header_not_before_comment(self):
        entry = '; comment\n2024-01-01 * "Payee" "Narr"\n  Assets:Cash  10 USD'
        result = self.bot.ensure_datetime_metadata(entry, "2024-01-01 10:00:00")
        lines = result.splitlines()
        header_idx = next(i for i, l in enumerate(lines) if '2024-01-01 *' in l)
        datetime_idx = next(i for i, l in enumerate(lines) if 'datetime:' in l)
        self.assertEqual(datetime_idx, header_idx + 1)

    def test_empty_entry(self):
        self.assertEqual(self.bot.ensure_datetime_metadata("", "2024-01-01 10:00:00"), "")


class TestPreferCurrentAccount(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()
        self.accounts = [
            "Assets:Cash:Current",
            "Assets:Savings",
            "Liabilities:CreditCard",
            "Expenses:Food",
        ]

    def test_exact_match(self):
        self.assertEqual(
            self.bot.prefer_current_account("Assets:Savings", self.accounts),
            "Assets:Savings",
        )

    def test_adds_current_suffix(self):
        self.assertEqual(
            self.bot.prefer_current_account("Assets:Cash", self.accounts),
            "Assets:Cash:Current",
        )

    def test_no_current_for_liabilities(self):
        result = self.bot.prefer_current_account("Liabilities:CreditCard", self.accounts)
        self.assertEqual(result, "Liabilities:CreditCard")

    def test_unknown_account_returned_as_is(self):
        result = self.bot.prefer_current_account("Assets:Unknown", self.accounts)
        self.assertEqual(result, "Assets:Unknown")

    def test_case_insensitive(self):
        result = self.bot.prefer_current_account("assets:savings", self.accounts)
        self.assertEqual(result, "Assets:Savings")


class TestExtractAccounts(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()

    def test_extracts_accounts(self):
        entry = (
            "2024-01-01 * \"Payee\" \"Narr\"\n"
            "  Assets:Cash:Current   -100 USD\n"
            "  Expenses:Food          100 USD\n"
        )
        accounts = self.bot.extract_accounts_from_entry(entry)
        self.assertIn("Assets:Cash:Current", accounts)
        self.assertIn("Expenses:Food", accounts)

    def test_skips_header(self):
        entry = '2024-01-01 * "Payee" "Narr"\n  Assets:Cash  10 USD'
        accounts = self.bot.extract_accounts_from_entry(entry)
        self.assertNotIn('2024-01-01', accounts)


class TestAddNonPnlAccounts(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()

    def test_skips_expenses_and_income(self):
        entry = "2024-01-01 * \"P\" \"N\"\n  Assets:Cash  -100 USD\n  Expenses:Food  100 USD\n"
        result = self.bot.add_non_pnl_accounts_to_commit_message("msg\n\n", entry)
        self.assertIn("Assets:Cash", result)
        self.assertNotIn("Expenses:Food", result)

    def test_includes_assets(self):
        entry = "2024-01-01 * \"P\" \"N\"\n  Assets:Bank  -100 USD\n  Income:Salary  100 USD\n"
        result = self.bot.add_non_pnl_accounts_to_commit_message("msg\n\n", entry)
        self.assertIn("Assets:Bank", result)
        self.assertNotIn("Income:Salary", result)


class TestIsPendingExpired(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()

    def test_not_expired(self):
        pending = {"created_at": time.time()}
        self.assertFalse(self.bot.is_pending_expired(pending))

    def test_expired(self):
        pending = {"created_at": time.time() - main.DRAFT_TTL_SECONDS - 1}
        self.assertTrue(self.bot.is_pending_expired(pending))


class TestBuildReviewButtons(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()

    def test_buttons_contain_pending_id(self):
        buttons = self.bot.build_review_buttons("42")
        kb = buttons["inline_keyboard"][0]
        texts = [b["text"] for b in kb]
        datas = [b["callback_data"] for b in kb]
        self.assertIn("✅", texts)
        self.assertIn("❌", texts)
        self.assertTrue(any("42" in d for d in datas))


class TestNextPendingId(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()

    def test_increments(self):
        id1 = self.bot.next_pending_id()
        id2 = self.bot.next_pending_id()
        self.assertEqual(int(id2), int(id1) + 1)

    def test_thread_safe_no_duplicates(self):
        results = []
        lock = threading.Lock()

        def worker():
            pid = self.bot.next_pending_id()
            with lock:
                results.append(pid)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), len(set(results)), "Duplicate pending IDs generated")


class TestNormalizeAndValidateLLMEntry(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()
        self.accounts = ["Assets:Cash:Current", "Expenses:Food", "Assets:Savings", "Liabilities:CC"]

    def _entry(self, header, *postings):
        return "\n".join([header] + list(postings))

    # --- Happy paths ---

    def test_valid_same_currency(self):
        entry = self._entry(
            '2024-01-01 * "Payee" "Narr"',
            "  Expenses:Food  50 USD",
            "  Assets:Cash:Current  -50 USD",
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, self.accounts)
        self.assertIn("Expenses:Food", result)
        self.assertIn("Assets:Cash:Current", result)

    def test_strips_code_fence(self):
        entry = '```\n2024-01-01 * "P" "N"\n  Expenses:Food  10 USD\n  Assets:Cash:Current  -10 USD\n```'
        result = self.bot.normalize_and_validate_llm_entry(entry, self.accounts)
        self.assertNotIn("```", result)

    def test_auto_inserts_fx_rate_abs0_larger(self):
        # abs0=100 CNY > abs1=14 USD  → rate annotated on posting[1]
        entry = self._entry(
            '2024-01-01 * "P" "N"',
            "  Assets:Cash:Current  100 CNY",
            "  Assets:Savings  -14 USD",
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, self.accounts)
        self.assertIn("@", result)

    def test_auto_inserts_fx_rate_abs1_larger(self):
        # abs0=14 USD < abs1=100 CNY → rate annotated on posting[0]
        entry = self._entry(
            '2024-01-01 * "P" "N"',
            "  Assets:Savings  14 USD",
            "  Assets:Cash:Current  -100 CNY",
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, self.accounts)
        self.assertIn("@", result)

    def test_existing_at_annotation_not_overwritten(self):
        entry = self._entry(
            '2024-01-01 * "P" "N"',
            "  Assets:Savings  14 USD @ 7.14 CNY",
            "  Assets:Cash:Current  -100 CNY",
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, self.accounts)
        self.assertIn("7.14", result)

    def test_prefer_current_account_applied(self):
        entry = self._entry(
            '2024-01-01 * "P" "N"',
            "  Expenses:Food  50 USD",
            "  Assets:Cash  -50 USD",
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, self.accounts)
        self.assertIn("Assets:Cash:Current", result)

    def test_metadata_lines_preserved(self):
        entry = self._entry(
            '2024-01-01 * "P" "N"',
            '  mykey: "myval"',
            "  Expenses:Food  50 USD",
            "  Assets:Cash:Current  -50 USD",
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, self.accounts)
        self.assertIn('mykey: "myval"', result)

    def test_float_balance_tolerance(self):
        # 0.1 + (-0.1) in floating point is exactly 0.0 in Python, but test tolerance path
        entry = self._entry(
            '2024-01-01 * "P" "N"',
            "  Expenses:Food  0.1 USD",
            "  Assets:Cash:Current  -0.1 USD",
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, self.accounts)
        self.assertIn("Expenses:Food", result)

    # --- Error paths ---

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            self.bot.normalize_and_validate_llm_entry("2024-01-01 * \"P\" \"N\"", self.accounts)

    def test_fewer_than_two_postings_raises(self):
        entry = self._entry(
            '2024-01-01 * "P" "N"',
            '  mykey: "val"',
            '  anothermeta: "val2"',
        )
        with self.assertRaises(ValueError):
            self.bot.normalize_and_validate_llm_entry(entry, self.accounts)

    def test_same_sign_postings_raises(self):
        entry = self._entry(
            '2024-01-01 * "P" "N"',
            "  Expenses:Food  50 USD",
            "  Assets:Cash:Current  10 USD",
        )
        with self.assertRaises(ValueError, msg="same sign should raise"):
            self.bot.normalize_and_validate_llm_entry(entry, self.accounts)

    def test_unbalanced_same_currency_raises(self):
        entry = self._entry(
            '2024-01-01 * "P" "N"',
            "  Expenses:Food  50 USD",
            "  Assets:Cash:Current  -40 USD",
        )
        with self.assertRaises(ValueError):
            self.bot.normalize_and_validate_llm_entry(entry, self.accounts)

    def test_cross_currency_zero_amount_raises(self):
        # Bug 2 fix: abs0>0, abs1==0 should raise, not produce "@@ 0 CURRENCY"
        entry = self._entry(
            '2024-01-01 * "P" "N"',
            "  Assets:Savings  100 CNY",
            "  Assets:Cash:Current  0 USD",
        )
        with self.assertRaises(ValueError, msg="zero cross-currency amount should raise"):
            self.bot.normalize_and_validate_llm_entry(entry, self.accounts)

    def test_multi_posting_imbalance_raises(self):
        entry = self._entry(
            '2024-01-01 * "P" "N"',
            "  Expenses:Food  50 USD",
            "  Expenses:Food  20 USD",
            "  Assets:Cash:Current  -60 USD",
        )
        with self.assertRaises(ValueError):
            self.bot.normalize_and_validate_llm_entry(entry, self.accounts)


class TestCleanupExpiredDrafts(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()
        self.bot.send_message = MagicMock()

    def test_removes_expired_entries(self):
        self.bot.pending_llm_entries["1"] = {
            "chat_id": 100,
            "created_at": time.time() - main.DRAFT_TTL_SECONDS - 5,
        }
        self.bot.cleanup_expired_drafts()
        self.assertNotIn("1", self.bot.pending_llm_entries)

    def test_keeps_fresh_entries(self):
        self.bot.pending_llm_entries["2"] = {
            "chat_id": 100,
            "created_at": time.time(),
        }
        self.bot.cleanup_expired_drafts()
        self.assertIn("2", self.bot.pending_llm_entries)

    def test_sends_expiry_message(self):
        self.bot.pending_llm_entries["3"] = {
            "chat_id": 999,
            "created_at": time.time() - main.DRAFT_TTL_SECONDS - 5,
        }
        self.bot.cleanup_expired_drafts()
        self.bot.send_message.assert_called_once()
        call_args = self.bot.send_message.call_args[0]
        self.assertEqual(call_args[0], 999)

    def test_cleans_up_decline_reason_bindings(self):
        self.bot.pending_llm_entries["4"] = {
            "chat_id": 100,
            "created_at": time.time() - main.DRAFT_TTL_SECONDS - 5,
        }
        self.bot.pending_decline_reasons[100] = "4"
        self.bot.cleanup_expired_drafts()
        self.assertNotIn(100, self.bot.pending_decline_reasons)


class TestRemoveDeclineReasonBindings(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()

    def test_removes_binding(self):
        self.bot.pending_decline_reasons[100] = "5"
        self.bot.pending_decline_reasons[200] = "6"
        self.bot.remove_decline_reason_bindings("5")
        self.assertNotIn(100, self.bot.pending_decline_reasons)
        self.assertIn(200, self.bot.pending_decline_reasons)

    def test_no_error_when_nothing_to_remove(self):
        self.bot.remove_decline_reason_bindings("nonexistent")


class TestPollingResilience(unittest.TestCase):
    """A failing getUpdates must not kill the bot, and must not spin.

    Replaces an older test that asserted the stop event gets set on a network
    exception — that was the loading-spinner's event, and the spinner is gone.
    Setting the bot's stop event on a network blip is exactly the bug fixed here.
    """

    def setUp(self):
        self.bot = make_bot()
        self.waits = []
        self.bot.stop.wait = lambda d: self.waits.append(d)

    def _resp(self, status=200, body=None, bad_json=False):
        r = MagicMock()
        r.status_code = status
        if bad_json:
            r.json.side_effect = ValueError("not json")
        else:
            r.json.return_value = body if body is not None else {"result": []}
        return r

    def test_network_exception_does_not_stop_bot(self):
        with patch.object(main.HTTP, "get", side_effect=ConnectionError("timeout")):
            result = self.bot.get_updates()
        self.assertEqual(result, {"result": []})
        self.assertFalse(self.bot.stop.is_set())

    def test_repeated_failures_back_off_exponentially(self):
        with patch.object(main.HTTP, "get", return_value=self._resp(500)):
            for _ in range(4):
                self.bot.get_updates()
        self.assertEqual(self.waits, [1.0, 2.0, 4.0, 8.0])

    def test_backoff_resets_after_success(self):
        seq = [self._resp(500), self._resp(500), self._resp(200), self._resp(500)]
        with patch.object(main.HTTP, "get", side_effect=seq):
            for _ in range(4):
                self.bot.get_updates()
        self.assertEqual(self.waits, [1.0, 2.0, 1.0])

    def test_429_honours_retry_after(self):
        r = self._resp(429, {"parameters": {"retry_after": 17}})
        with patch.object(main.HTTP, "get", return_value=r):
            self.bot.get_updates()
        self.assertEqual(self.waits, [17.0])

    def test_malformed_payloads_do_not_raise(self):
        for label, r in [("non-JSON", self._resp(200, bad_json=True)),
                         ("no result key", self._resp(200, {"ok": False})),
                         ("result not a list", self._resp(200, {"result": "nope"}))]:
            with self.subTest(payload=label):
                with patch.object(main.HTTP, "get", return_value=r):
                    self.assertEqual(self.bot.get_updates(), {"result": []})

    def test_start_survives_a_crashing_poll_cycle(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("network hiccup")
            self.bot.stop.set()

        self.bot.process_updates = flaky
        self.bot._backoff = lambda *a, **k: None
        self.bot.start()
        self.assertEqual(len(calls), 3)


class TestUpdateIdAck(unittest.TestCase):
    """The offset must never rewind: a batch is acked at its max update_id."""

    def setUp(self):
        self.bot = make_bot()
        self.bot.cleanup_expired_drafts = MagicMock()
        self.bot._spawn_handler = MagicMock()

    def _run(self, results):
        self.bot.get_updates = MagicMock(return_value={"result": results})
        self.bot.process_updates()
        return self.bot.update_id

    def test_message_before_callback(self):
        got = self._run([
            {"update_id": 1, "message": {"text": "x", "chat": {"id": 123}}},
            {"update_id": 2, "callback_query": {"id": "c", "data": "a:b", "message": {"chat": {"id": 123}}}},
        ])
        self.assertEqual(got, 2)

    def test_callback_before_message(self):
        got = self._run([
            {"update_id": 1, "callback_query": {"id": "c", "data": "a:b", "message": {"chat": {"id": 123}}}},
            {"update_id": 2, "message": {"text": "x", "chat": {"id": 123}}},
        ])
        self.assertEqual(got, 2)

    def test_empty_batch_leaves_offset_alone(self):
        self.assertEqual(self._run([]), 0)


class TestHandlerCrashIsolation(unittest.TestCase):
    """A crashing handler must report back, not die silently in its thread."""

    def _spawn_and_wait(self, bot, chat_id):
        bot.handle_message = MagicMock(side_effect=RuntimeError("boom"))
        bot._spawn_handler(bot.handle_message, {"message": {"chat": {"id": chat_id}}}, chat_id)
        time.sleep(0.15)

    def test_authorized_user_is_told(self):
        bot = make_bot()
        bot.send_message = MagicMock()
        self._spawn_and_wait(bot, 123)
        self.assertEqual(bot.send_message.call_count, 1)

    def test_unauthorized_user_gets_nothing(self):
        bot = make_bot()
        bot.send_message = MagicMock()
        self._spawn_and_wait(bot, 999)
        self.assertEqual(bot.send_message.call_count, 0)

    def test_failure_to_notify_does_not_raise(self):
        bot = make_bot()
        bot.send_message = MagicMock(side_effect=RuntimeError("telegram down"))
        self._spawn_and_wait(bot, 123)


class TestAuthorizationGate(unittest.TestCase):
    """CHAT_ID is default-deny: empty means nobody, not everybody."""

    def test_empty_chat_id_refuses_to_start(self):
        with patch.object(main, "ALLOWED_CHATS", set()):
            with self.assertRaises(ValueError):
                Bot()

    def test_is_authorized_matches_whitelist(self):
        with patch.object(main, "ALLOWED_CHATS", {"123", "456"}):
            self.assertTrue(main.is_authorized(123))
            self.assertTrue(main.is_authorized("456"))
            self.assertFalse(main.is_authorized(999))
            self.assertFalse(main.is_authorized(None))

    def test_unauthorized_message_is_dropped_silently(self):
        with patch.object(main, "ALLOWED_CHATS", {"123"}):
            bot = make_bot()
            bot.send_message = MagicMock()
            bot.parse_accounts = MagicMock(side_effect=AssertionError("must not reach GitHub"))
            bot.handle_message({"message": {"text": "/last 50", "chat": {"id": 999}}})
            self.assertEqual(bot.send_message.call_count, 0)

    def test_unauthorized_callback_cannot_claim_a_pending_entry(self):
        with patch.object(main, "ALLOWED_CHATS", {"123"}):
            bot = make_bot()
            bot.answer_callback_query = MagicMock()
            bot._github_put_file = MagicMock(side_effect=AssertionError("must not write repo"))
            pid = bot.next_pending_id()
            bot.pending_llm_entries[pid] = bot._make_pending_entry(999, "x", "m", "u", "2024-01-01")
            bot.handle_callback_query({"callback_query": {
                "id": "cb", "data": f"approve:{pid}",
                "message": {"chat": {"id": 999}, "message_id": 1}}})
            self.assertEqual(bot.answer_callback_query.call_count, 0)
            self.assertIn(pid, bot.pending_llm_entries)


class FakeGitHub:
    """Mimics GitHub's sha-conflict semantics: a PUT carrying a stale sha gets 409."""

    def __init__(self, put_delay=0.0):
        self.content = "; ledger"
        self.sha = "sha0"
        self.commits = []
        self.rejections = 0
        self.put_delay = put_delay
        self._lock = threading.Lock()

    def download(self, file_path=None):
        with self._lock:
            return {"content": self.content, "sha": self.sha}

    def put(self, content, sha, msg, file_path=None):
        if self.put_delay:
            time.sleep(self.put_delay)
        with self._lock:
            if sha != self.sha:
                self.rejections += 1
                return False, 409
            self.content = content
            self.sha = f"sha{len(self.commits) + 1}"
            self.commits.append(msg)
            return True, 200


class TestAppendToFile(unittest.TestCase):
    """Appends must survive a concurrent write instead of failing the user."""

    def setUp(self):
        self.bot = make_bot()
        self.gh = FakeGitHub()
        self.bot.github_download_file = self.gh.download
        self.bot._github_put_file = self.gh.put

    def test_plain_append(self):
        ok, err = self.bot.append_to_file("ENTRY", "msg")
        self.assertTrue(ok)
        self.assertIn("ENTRY", self.gh.content)

    def test_conflict_is_retried_against_fresh_content(self):
        real_put, calls = self.gh.put, {"n": 0}

        def conflict_once(content, sha, msg, file_path=None):
            calls["n"] += 1
            return (False, 409) if calls["n"] == 1 else real_put(content, sha, msg, file_path)

        self.bot._github_put_file = conflict_once
        ok, _ = self.bot.append_to_file("ENTRY", "msg")
        self.assertTrue(ok)
        self.assertIn("ENTRY", self.gh.content)

    def test_persistent_conflict_gives_up_with_a_message(self):
        self.bot._github_put_file = lambda *a, **k: (False, 409)
        ok, err = self.bot.append_to_file("ENTRY", "msg")
        self.assertFalse(ok)
        self.assertTrue(err)

    def test_non_conflict_error_is_not_retried(self):
        calls = {"n": 0}

        def put_500(*a, **k):
            calls["n"] += 1
            return False, 500

        self.bot._github_put_file = put_500
        ok, _ = self.bot.append_to_file("ENTRY", "msg")
        self.assertFalse(ok)
        self.assertEqual(calls["n"], 1)

    def test_download_failure_does_not_upload(self):
        self.bot.github_download_file = lambda *a, **k: None
        ok, err = self.bot.append_to_file("ENTRY", "msg")
        self.assertFalse(ok)
        self.assertEqual(self.gh.commits, [])

    def test_provided_download_avoids_second_fetch(self):
        self.bot.github_download_file = MagicMock(side_effect=AssertionError("should not refetch"))
        ok, _ = self.bot.append_to_file("ENTRY", "msg", downloaded={"content": "c", "sha": "sha0"})
        self.assertTrue(ok)


class TestApproveCommitWindow(unittest.TestCase):
    """A reviewed draft must not evaporate because GitHub blipped."""

    def setUp(self):
        self.bot = make_bot()
        self.bot.send_message = MagicMock()
        self.bot.answer_callback_query = MagicMock()
        self.bot.edit_message_reply_markup = MagicMock()
        self.gh = FakeGitHub()
        self.bot.github_download_file = self.gh.download
        self.bot._github_put_file = self.gh.put

    def _pending(self, appendix="ENTRY"):
        pid = self.bot.next_pending_id()
        self.bot.pending_llm_entries[pid] = self.bot._make_pending_entry(
            123, appendix, "msg", "input", "2024-01-01")
        return pid

    def _approve(self, pid):
        self.bot.handle_callback_query({"callback_query": {
            "id": "cb", "data": f"approve:{pid}",
            "message": {"chat": {"id": 123}, "message_id": 1}}})

    def test_success_consumes_draft_and_clears_buttons(self):
        pid = self._pending()
        self._approve(pid)
        self.assertNotIn(pid, self.bot.pending_llm_entries)
        self.assertIn("ENTRY", self.gh.content)
        self.assertEqual(self.bot.edit_message_reply_markup.call_count, 1)

    def test_failure_restores_draft_and_keeps_buttons(self):
        self.bot._github_put_file = lambda *a, **k: (False, 500)
        pid = self._pending()
        self._approve(pid)
        self.assertIn(pid, self.bot.pending_llm_entries)
        self.assertEqual(self.bot.edit_message_reply_markup.call_count, 0)

    def test_restored_draft_ttl_is_refreshed(self):
        self.bot._github_put_file = lambda *a, **k: (False, 500)
        pid = self._pending()
        self.bot.pending_llm_entries[pid]["created_at"] = time.time() - main.DRAFT_TTL_SECONDS + 1
        self._approve(pid)
        self.assertFalse(self.bot.is_pending_expired(self.bot.pending_llm_entries[pid]))

    def test_concurrent_approvals_both_land(self):
        self.gh.put_delay = 0.05
        pids = [self._pending(f"CONCURRENT-{i}") for i in range(2)]
        threads = [threading.Thread(target=self._approve, args=(p,)) for p in pids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for i in range(2):
            self.assertIn(f"CONCURRENT-{i}", self.gh.content)
        self.assertEqual(len(self.gh.commits), 2)


class TestExtractJsonObject(unittest.TestCase):
    """The router's JSON must survive code fences, prose, and braces inside strings."""

    def test_plain(self):
        self.assertEqual(main.extract_json_object('{"intent": "entry"}'), {"intent": "entry"})

    def test_code_fence(self):
        raw = '```json\n{"intent": "query", "bql": "SELECT date"}\n```'
        self.assertEqual(main.extract_json_object(raw), {"intent": "query", "bql": "SELECT date"})

    def test_surrounding_prose(self):
        self.assertEqual(main.extract_json_object('好的：\n{"intent": "entry"}\n希望有帮助'),
                         {"intent": "entry"})

    def test_brace_inside_string(self):
        raw = '{"bql": "SELECT x WHERE a ~ \\"{brace}\\"", "intent": "query"}'
        self.assertEqual(main.extract_json_object(raw),
                         {"bql": 'SELECT x WHERE a ~ "{brace}"', "intent": "query"})

    def test_unparseable_returns_none(self):
        for raw in ("no json", "", "{broken", "[1,2]", None):
            with self.subTest(raw=raw):
                self.assertIsNone(main.extract_json_object(raw))


class TestQueryRendering(unittest.TestCase):
    """Columns are sized by display width; beancount's renderer miscounts CJK."""

    def test_display_width_counts_cjk_as_two(self):
        self.assertEqual(main.display_width("abc"), 3)
        self.assertEqual(main.display_width("咖啡"), 4)
        self.assertEqual(main.display_width("a咖"), 3)

    def test_empty_result(self):
        self.assertEqual(main.format_query_result([("date", str)], []), "(no results)")

    def test_columns_sized_by_display_width(self):
        rtypes = [("date", str), ("narration", str)]
        rows = [("2026-07-02", "买菜和日用品"), ("2026-07-01", "咖啡")]
        lines = main.format_query_result(rtypes, rows).splitlines()
        widths = [len(s) for s in lines[1].split("  ")]
        self.assertEqual(widths, [10, 12])  # 12 = display width of 买菜和日用品, not 6

    def test_no_trailing_whitespace(self):
        out = main.format_query_result([("a", str), ("b", str)], [("x", "yyy"), ("zz", "w")])
        for line in out.splitlines():
            self.assertEqual(line, line.rstrip())

    def test_long_result_is_truncated(self):
        rows = [(str(i), "x" * 60) for i in range(500)]
        out = main.format_query_result([("n", str), ("t", str)], rows, max_chars=1000)
        self.assertLessEqual(len(out), 1000)
        self.assertIn("more rows omitted", out)


LEDGER_ACCOUNTS = (
    "1970-01-01 open Expenses:Food GBP\n"
    "1970-01-01 open Expenses:Education GBP\n"
    "1970-01-01 open Assets:Bank:HSBC:Current GBP\n"
    "1970-01-01 open Liabilities:CreditCard:Chase GBP\n"
)
LEDGER_JOURNAL = '''
2026-06-20 * "Tesco" "上月买菜"
  Expenses:Food           30.00 GBP
  Liabilities:CreditCard:Chase

2026-07-01 * "Starbucks" "咖啡"
  Expenses:Food            3.50 GBP
  Liabilities:CreditCard:Chase

2026-07-05 * "Amazon" "书"
  Expenses:Education      15.00 GBP
  Assets:Bank:HSBC:Current
'''


def _ledger_download(file_path="test.bean"):
    if file_path.startswith("accounts/"):
        keep = file_path.endswith(("expenses.bean", "assets.bean", "liabilities.bean"))
        return {"content": LEDGER_ACCOUNTS if keep else "", "sha": "s"}
    return {"content": LEDGER_JOURNAL, "sha": "s"}


class TestLedgerQuery(unittest.TestCase):
    """load_ledger concatenates account files with the journal for beancount's loader."""

    def setUp(self):
        self.bot = make_bot()
        self.bot.github_download_file = _ledger_download

    def test_load_ledger_parses(self):
        entries, options_map = self.bot.load_ledger()
        self.assertTrue(entries)
        self.assertIn("dcontext", options_map)

    def test_load_ledger_returns_none_without_journal(self):
        self.bot.github_download_file = lambda p="x": None
        self.assertIsNone(self.bot.load_ledger())

    def test_run_bql_filters(self):
        _, rrows = self.bot.run_bql('SELECT date WHERE account ~ "Chase" ORDER BY date DESC')
        self.assertEqual(len(rrows), 2)

    def test_run_bql_aggregates(self):
        _, rrows = self.bot.run_bql(
            'SELECT sum(position) WHERE account ~ "Expenses:Food" AND year=2026 AND month=7')
        self.assertIn("3.50", str(rrows[0]))

    def test_bad_bql_raises(self):
        with self.assertRaises(Exception):
            self.bot.run_bql("SELECT bogus_column")


class TestIntentRouting(unittest.TestCase):
    """Routing must never block the bot's primary job: on any doubt, treat as entry."""

    def _bot(self, *replies):
        b = make_bot()
        b._accounts_for_prompt = lambda: ["Expenses:Food (GBP)"]
        b._call_llm_backends = MagicMock(side_effect=list(replies))
        return b

    def test_query(self):
        b = self._bot('{"intent": "query", "bql": "SELECT date"}')
        self.assertEqual(b.route_intent("最近10条", "2026-07-17")["intent"], "query")

    def test_entry(self):
        b = self._bot('{"intent": "entry"}')
        self.assertEqual(b.route_intent("星巴克 35", "2026-07-17")["intent"], "entry")

    def test_llm_failure_falls_back_to_entry(self):
        self.assertEqual(self._bot(RuntimeError("down")).route_intent("x", "2026-07-17"),
                         {"intent": "entry"})

    def test_garbage_falls_back_to_entry(self):
        self.assertEqual(self._bot("I think query").route_intent("x", "2026-07-17"),
                         {"intent": "entry"})

    def test_query_without_bql_falls_back(self):
        self.assertEqual(self._bot('{"intent": "query"}').route_intent("x", "2026-07-17"),
                         {"intent": "entry"})


class TestAnswerQueryRetry(unittest.TestCase):
    """A bad BQL is fed back to the LLM, mirroring the beancount-syntax retry loop."""

    def _bot(self, *replies):
        b = make_bot()
        b.github_download_file = _ledger_download
        b._accounts_for_prompt = lambda: ["Expenses:Food (GBP)"]
        b._call_llm_backends = MagicMock(side_effect=list(replies))
        return b

    def test_good_bql_runs_directly(self):
        b = self._bot()
        _, out = b.answer_query("q", 'SELECT date WHERE account ~ "Chase"', "2026-07-17")
        self.assertIn("2026-07", out)
        self.assertEqual(b._call_llm_backends.call_count, 0)

    def test_bad_bql_is_repaired(self):
        b = self._bot('{"intent":"query","bql":"SELECT date WHERE account ~ \\"Chase\\""}')
        bql, out = b.answer_query("q", "SELECT bogus", "2026-07-17")
        self.assertIn("2026-07", out)
        self.assertNotIn("bogus", bql)

    def test_retries_bounded(self):
        b = self._bot(*(['{"intent":"query","bql":"SELECT still_bad"}'] * 5))
        with self.assertRaises(ValueError):
            b.answer_query("q", "SELECT bad", "2026-07-17")
        self.assertLessEqual(b._call_llm_backends.call_count, main.MAX_BEANCOUNT_RETRIES)

    def test_download_failure_not_retried_at_llm(self):
        b = self._bot()
        b.github_download_file = lambda p="x": None
        with self.assertRaises(ValueError):
            b.answer_query("q", "SELECT date", "2026-07-17")
        self.assertEqual(b._call_llm_backends.call_count, 0)


class TestQueryEndToEnd(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()
        self.bot.llm_enabled = True
        self.bot.send_message = MagicMock()
        self.bot.github_download_file = _ledger_download
        self.bot.parse_accounts = lambda: ["Expenses:Food", "Liabilities:CreditCard:Chase"]
        self.bot._accounts_for_prompt = lambda: ["Expenses:Food (GBP)"]

    def test_query_answers_without_a_draft(self):
        self.bot._call_llm_backends = MagicMock(return_value=(
            '{"intent": "query", "bql": "SELECT date, payee, position '
            'WHERE account ~ \\"Chase\\" ORDER BY date DESC LIMIT 10"}'))
        self.bot.handle_message({"message": {"text": "列出最近的 chase 记录", "chat": {"id": 123}}})
        sent = "\n".join(str(c) for c in self.bot.send_message.call_args_list)
        self.assertIn("Tesco", sent)
        self.assertIn("SELECT", sent)
        self.assertEqual(len(self.bot.pending_llm_entries), 0)

    def test_entry_still_creates_a_draft(self):
        self.bot._call_llm_backends = MagicMock(return_value='{"intent": "entry"}')
        self.bot.call_openai_compatible = MagicMock(
            return_value='2026-07-17 * "S" "咖啡"\n  Expenses:Food  3.50 GBP\n  Liabilities:CreditCard:Chase')
        self.bot.add_non_pnl_accounts_to_commit_message = lambda m, a: m
        self.bot.build_review_buttons = lambda p: {}
        self.bot.handle_message({"message": {"text": "星巴克 3.5", "chat": {"id": 123}}})
        self.assertEqual(len(self.bot.pending_llm_entries), 1)


class TestDateParsing(unittest.TestCase):
    """Bug 3: custom date must extract only YYYY-MM-DD, not trailing text."""

    def setUp(self):
        self.bot = make_bot()
        self.bot.llm_enabled = True
        self.bot.send_message = MagicMock()
        # Stub out everything that would make a real network call
        self.bot.parse_accounts = MagicMock(return_value=["Assets:Cash:Current", "Expenses:Food"])
        self.bot.call_openai_compatible = MagicMock(
            return_value='2024-01-15 * "P" "N"\n  Expenses:Food  10 USD\n  Assets:Cash:Current  -10 USD'
        )
        self.bot.prepend_natural_language_comment = MagicMock(side_effect=lambda e, _: e)
        self.bot.add_non_pnl_accounts_to_commit_message = MagicMock(side_effect=lambda m, _: m)
        self.bot.next_pending_id = MagicMock(return_value="99")

    def _make_message(self, text):
        return {"message": {"text": text, "chat": {"id": 123}}}

    def test_clean_date_prefix_is_used(self):
        """'2024-01-15\\nmy transaction' (clean date line) must use date_str='2024-01-15'."""
        call_args_store = {}

        def mock_call_llm(text, accounts, date_str, **kwargs):
            call_args_store['date_str'] = date_str
            return '2024-01-15 * "P" "N"\n  Expenses:Food  10 USD\n  Assets:Cash:Current  -10 USD'

        self.bot.call_openai_compatible = mock_call_llm
        self.bot.handle_message(self._make_message("2024-01-15\nmy transaction"))

        self.assertEqual(call_args_store.get('date_str'), '2024-01-15')

    def test_date_with_trailing_text_not_parsed_as_date(self):
        """'2024-01-15 extra\\nmy transaction': first line fails strptime, date is NOT extracted."""
        call_args_store = {}

        def mock_call_llm(text, accounts, date_str, **kwargs):
            call_args_store['date_str'] = date_str
            return '2026-03-10 * "P" "N"\n  Expenses:Food  10 USD\n  Assets:Cash:Current  -10 USD'

        self.bot.call_openai_compatible = mock_call_llm
        self.bot.handle_message(self._make_message("2024-01-15 extra text here\nmy transaction"))

        # strptime rejects "2024-01-15 extra text here", so date_str falls back to today
        self.assertNotEqual(call_args_store.get('date_str'), '2024-01-15')


class TestConcurrentPendingId(unittest.TestCase):
    """Bug 4: next_pending_id must be thread-safe."""

    def test_no_duplicate_ids_under_concurrent_access(self):
        bot = make_bot()
        ids = []
        lock = threading.Lock()

        def worker():
            pid = bot.next_pending_id()
            with lock:
                ids.append(pid)

        threads = [threading.Thread(target=worker) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(ids), len(set(ids)))


class TestPopPending(unittest.TestCase):
    """Bug 4: _pop_pending must atomically remove and return entry."""

    def setUp(self):
        self.bot = make_bot()

    def test_returns_entry(self):
        self.bot.pending_llm_entries["10"] = {"chat_id": 1}
        result = self.bot._pop_pending("10")
        self.assertEqual(result, {"chat_id": 1})
        self.assertNotIn("10", self.bot.pending_llm_entries)

    def test_returns_none_if_missing(self):
        result = self.bot._pop_pending("nonexistent")
        self.assertIsNone(result)

    def test_concurrent_pop_only_one_succeeds(self):
        self.bot.pending_llm_entries["20"] = {"chat_id": 2}
        results = []
        lock = threading.Lock()

        def worker():
            r = self.bot._pop_pending("20")
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        non_none = [r for r in results if r is not None]
        self.assertEqual(len(non_none), 1, "Exactly one thread should claim the pending entry")


if __name__ == "__main__":
    unittest.main(verbosity=2)
