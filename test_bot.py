"""Unit tests for main.py Bot logic."""

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


class TestPrependComment(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()

    def test_prepends_comment(self):
        entry = "2024-01-01 * \"Payee\" \"Narr\"\n  Assets:Cash  10 USD\n  Expenses:Food  -10 USD"
        result = self.bot.prepend_natural_language_comment(entry, "lunch 10 usd")
        self.assertTrue(result.startswith("; lunch 10 usd\n"))

    def test_no_duplicate_comment(self):
        entry = "; lunch 10 usd\n2024-01-01 * \"Payee\" \"Narr\""
        result = self.bot.prepend_natural_language_comment(entry, "lunch 10 usd")
        self.assertEqual(result.count("; lunch 10 usd"), 1)

    def test_empty_input_no_change(self):
        entry = "2024-01-01 * \"Payee\" \"Narr\""
        result = self.bot.prepend_natural_language_comment(entry, "")
        self.assertEqual(result, entry)

    def test_multiline_input_normalized(self):
        entry = "2024-01-01 * \"Payee\" \"Narr\""
        result = self.bot.prepend_natural_language_comment(entry, "line1\nline2")
        self.assertTrue(result.startswith("; line1 line2\n"))


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


class TestLoadingThreadLeak(unittest.TestCase):
    """Bug 1: loading thread must be stopped even on network exception."""

    def test_stop_event_set_on_exception(self):
        bot = make_bot()
        stop_events_set = []

        original_set = threading.Event.set

        def tracking_set(self_event):
            stop_events_set.append(True)
            original_set(self_event)

        with patch("requests.get", side_effect=ConnectionError("timeout")):
            with patch.object(threading.Event, "set", tracking_set):
                result = bot.get_updates()

        self.assertEqual(result, {"result": []})
        # The stop event must have been set (spinner stopped)
        self.assertGreater(len(stop_events_set), 0)


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
