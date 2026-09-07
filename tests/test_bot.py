"""Unit tests for main.py Bot logic."""

# Run from anywhere: put the repo root on the path so `from beancounter import bot as main` resolves.
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

# Importing main is side-effect free; tests pass their own settings.
from beancounter import bot as main
from beancounter.bot import Bot


def make_bot() -> Bot:
    return Bot(settings=MOCK_CONFIG, state_path=":memory:")


class TestStripCodeFence(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()
        self.addCleanup(self.bot.close)

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
        self.addCleanup(self.bot.close)

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
        self.addCleanup(self.bot.close)
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
        self.addCleanup(self.bot.close)

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
        self.addCleanup(self.bot.close)

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
        self.addCleanup(self.bot.close)

    def test_not_expired(self):
        pending = {"created_at": time.time()}
        self.assertFalse(self.bot.is_pending_expired(pending))

    def test_expired(self):
        pending = {"created_at": time.time() - main.DRAFT_TTL_SECONDS - 1}
        self.assertTrue(self.bot.is_pending_expired(pending))


class TestBuildReviewButtons(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()
        self.addCleanup(self.bot.close)

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
        self.addCleanup(self.bot.close)

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


class TestExamplesForPayee(unittest.TestCase):
    """Route A: past same-payee entries injected into the draft prompt."""

    LEDGER = (
        "2026-01-01 open Expenses:Food:Coffee\n"
        "2026-01-01 open Assets:WalletA:Current\n"
        "2026-01-01 open Assets:Cash\n\n"
        '2026-03-10 * "示例甲咖啡" "生椰拿铁"\n'
        '  prompt: "示例甲 18"\n'
        "  Expenses:Food:Coffee   18.00 CNY\n"
        "  Assets:WalletA:Current\n\n"
        '2026-05-02 * "示例乙" "美式"\n'
        "  Expenses:Food:Coffee   30.00 CNY\n"
        "  Assets:Cash\n\n"
        '2026-06-20 * "示例甲" "拿铁"\n'
        "  Expenses:Food:Coffee   16.00 CNY\n"
        "  Assets:WalletA:Current\n"
    )

    def setUp(self):
        self.bot = make_bot()
        self.addCleanup(self.bot.close)
        from beancount import loader
        entries, _errors, opts = loader.load_string(self.LEDGER)
        self.bot.load_ledger = lambda: (entries, opts)

    def test_loose_substring_match(self):
        # "示例甲" matches both "示例甲" and "示例甲咖啡"; "示例乙" is excluded.
        out = self.bot.examples_for_payee("示例甲")
        self.assertIn('"示例甲咖啡"', out)
        self.assertIn('"示例甲"', out)
        self.assertNotIn("示例乙", out)

    def test_sorted_ascending_and_limited(self):
        out = self.bot.examples_for_payee("示例甲")
        self.assertLess(out.index("2026-03-10"), out.index("2026-06-20"))
        self.assertEqual(self.bot.examples_for_payee("示例甲", limit=1).count("Expenses:Food:Coffee"), 1)

    def test_metadata_stripped(self):
        # The prompt/datetime metadata this pipeline injects must not leak into examples.
        self.assertNotIn("prompt:", self.bot.examples_for_payee("示例甲"))

    def test_no_match_returns_none(self):
        self.assertIsNone(self.bot.examples_for_payee("示例丙"))

    def test_empty_payee_returns_none(self):
        self.assertIsNone(self.bot.examples_for_payee(""))
        self.assertIsNone(self.bot.examples_for_payee("   "))

    def test_ledger_unavailable_returns_none(self):
        self.bot.load_ledger = lambda: None
        self.assertIsNone(self.bot.examples_for_payee("示例甲"))

    def test_limit_boundary(self):
        # 11 matching entries, default limit keeps the 10 most recent (drops the oldest).
        from beancount import loader
        lines = ["2026-01-01 open Expenses:X\n", "2026-01-01 open Assets:Cash\n\n"]
        for d in range(1, 12):
            lines.append(f'2026-02-{d:02d} * "Foo" "n{d}"\n  Expenses:X  1 CNY\n  Assets:Cash\n\n')
        entries, _e, opts = loader.load_string("".join(lines))
        self.bot.load_ledger = lambda: (entries, opts)
        out = self.bot.examples_for_payee("Foo")
        self.assertEqual(out.count('* "Foo"'), 10)
        self.assertNotIn('"n1"', out)   # oldest dropped
        self.assertIn('"n11"', out)     # newest kept

    def test_cost_and_price_kept(self):
        # Commodity/FX examples must stay balanced: cost {...} and price @ are rendered.
        from beancount import loader
        entries, _e, opts = loader.load_string(
            "2026-01-01 open Assets:Broker\n2026-01-01 open Assets:Cash\n2026-01-01 open Assets:USD\n\n"
            '2026-04-01 * "Broker" "buy"\n  Assets:Broker  10 AAPL {150.00 USD}\n  Assets:Cash  -1500.00 USD\n\n'
            '2026-04-02 * "Broker" "fx"\n  Assets:USD  100 USD @ 7.10 CNY\n  Assets:Cash  -710.00 CNY\n')
        self.bot.load_ledger = lambda: (entries, opts)
        out = self.bot.examples_for_payee("Broker")
        self.assertIn("{150.00 USD}", out)
        self.assertIn("@ 7.10 CNY", out)

    def test_quotes_escaped_stay_valid_beancount(self):
        # A payee/narration containing double quotes must render as re-parseable beancount.
        import beancount.parser.parser as parser
        from beancount import loader
        entries, _e, opts = loader.load_string(
            "2026-01-01 open Expenses:Fun\n2026-01-01 open Assets:Cash\n\n"
            '2026-04-01 * "Steam \\"Sale\\"" "bought \\"HL\\""\n  Expenses:Fun  50 CNY\n  Assets:Cash\n')
        self.bot.load_ledger = lambda: (entries, opts)
        out = self.bot.examples_for_payee("Steam")
        self.assertIn('\\"Sale\\"', out)
        _entries, errors, _opts = parser.parse_string(out)
        self.assertEqual(errors, [])


class TestBuildUserPromptExamples(unittest.TestCase):
    """Route A: examples injected into the generation prompt, before the declined draft."""

    def test_examples_injected(self):
        from beancounter.prompts import build_user_prompt
        p = build_user_prompt("2026-07-21", ["Expenses:X"], "示例甲 20",
                              examples='2026-06-20 * "示例甲" "拿铁"\n  Expenses:X  16 CNY')
        self.assertIn("参考", p)
        self.assertIn('"示例甲"', p)

    def test_no_examples_leaves_prompt_unchanged(self):
        from beancounter.prompts import build_user_prompt
        self.assertNotIn("参考", build_user_prompt("2026-07-21", ["Expenses:X"], "打车 20"))
        self.assertNotIn("参考", build_user_prompt("2026-07-21", ["Expenses:X"], "打车 20", examples=""))

    def test_examples_precede_declined_draft(self):
        # The 参考 block must come before the previous declined draft so the correction
        # context is the last thing the model reads.
        from beancounter.prompts import build_user_prompt
        p = build_user_prompt("2026-07-21", ["Expenses:X"], "示例甲 20",
                              previous_draft="OLD_DRAFT", examples="EXAMPLE_BLOCK")
        self.assertLess(p.index("EXAMPLE_BLOCK"), p.index("OLD_DRAFT"))


class TestFrequentPayees(unittest.TestCase):
    """The top-N payee list handed to the generator so it reuses existing spellings."""

    LEDGER = (
        "2026-01-01 open Expenses:Food:Coffee\n"
        "2026-01-01 open Assets:Cash\n\n"
        '2026-03-10 * "示例甲咖啡" "a"\n  Expenses:Food:Coffee  1 CNY\n  Assets:Cash\n\n'
        '2026-03-11 * "示例甲咖啡" "b"\n  Expenses:Food:Coffee  1 CNY\n  Assets:Cash\n\n'
        '2026-03-12 * "示例甲咖啡" "c"\n  Expenses:Food:Coffee  1 CNY\n  Assets:Cash\n\n'
        '2026-05-02 * "示例乙" "d"\n  Expenses:Food:Coffee  1 CNY\n  Assets:Cash\n\n'
        '2026-05-03 * "示例乙" "e"\n  Expenses:Food:Coffee  1 CNY\n  Assets:Cash\n\n'
        '2026-06-20 * "示例丙" "f"\n  Expenses:Food:Coffee  1 CNY\n  Assets:Cash\n'
    )

    def _load(self, ledger):
        from beancount import loader
        entries, _e, opts = loader.load_string(ledger)
        return entries, opts

    def setUp(self):
        self.bot = make_bot()
        self.addCleanup(self.bot.close)
        self.entries, self.opts = self._load(self.LEDGER)
        self.bot.load_ledger = lambda: (self.entries, self.opts)

    def test_sorted_by_frequency(self):
        # 示例甲咖啡 x3 > 示例乙 x2 > 示例丙 x1
        self.assertEqual(self.bot.frequent_payees(), ["示例甲咖啡", "示例乙", "示例丙"])

    def test_limit_slices_top_n(self):
        self.assertEqual(self.bot.frequent_payees(limit=2), ["示例甲咖啡", "示例乙"])

    def test_ledger_unavailable_returns_empty(self):
        self.bot.load_ledger = lambda: None
        self.assertEqual(self.bot.frequent_payees(), [])

    def test_whitespace_collapsed_and_merged(self):
        # A payee written with interior double-space and its single-space twin collapse to
        # one token (re.sub \s+ → " ") and are counted together — otherwise the 、-joined
        # list handed to the LLM could break across the extra whitespace.
        entries, opts = self._load(
            "2026-01-01 open Expenses:X\n2026-01-01 open Assets:Cash\n\n"
            '2026-02-01 * "A  B" "n"\n  Expenses:X  1 CNY\n  Assets:Cash\n\n'
            '2026-02-02 * "A B" "n"\n  Expenses:X  1 CNY\n  Assets:Cash\n')
        self.bot.load_ledger = lambda: (entries, opts)
        self.assertEqual(self.bot.frequent_payees(), ["A B"])

    def test_none_payee_ignored(self):
        # A single-string directive is a narration with no payee; it must not appear.
        entries, opts = self._load(
            "2026-01-01 open Expenses:X\n2026-01-01 open Assets:Cash\n\n"
            '2026-02-01 * "转账"\n  Expenses:X  1 CNY\n  Assets:Cash\n\n'
            '2026-02-02 * "Foo" "n"\n  Expenses:X  1 CNY\n  Assets:Cash\n')
        self.bot.load_ledger = lambda: (entries, opts)
        self.assertEqual(self.bot.frequent_payees(), ["Foo"])

    def test_cached_by_entries_identity(self):
        # First call stashes against the current cache entries; a second call with the same
        # entries object serves from cache. Corrupt the stored list to prove no recount.
        self.bot._ledger_cache["entries"] = self.entries
        self.bot.frequent_payees()
        self.bot._ledger_cache["payees"] = ["SENTINEL"]
        self.assertEqual(self.bot.frequent_payees(), ["SENTINEL"])

    def test_no_stash_when_cache_describes_other_ledger(self):
        # Fallback path (cache entries is None, e.g. no tree sha): never stashes, so every
        # call recomputes rather than serving a list built from a different ledger.
        self.bot._ledger_cache["entries"] = None
        self.bot.frequent_payees()
        self.assertIsNone(self.bot._ledger_cache.get("payees"))

    def test_loaded_param_skips_reload(self):
        # Passing an already-loaded ledger avoids a second load_ledger round trip (P1).
        calls = {"n": 0}
        def counting_load():
            calls["n"] += 1
            return (self.entries, self.opts)
        self.bot.load_ledger = counting_load
        self.bot.frequent_payees(loaded=(self.entries, self.opts))
        self.bot.examples_for_payee("示例甲", loaded=(self.entries, self.opts))
        self.assertEqual(calls["n"], 0)


class TestHandlePhotoMessage(unittest.TestCase):
    """Photo entry point: invest-vs-expense routing and the download-failure path."""

    def setUp(self):
        self.bot = make_bot()
        self.addCleanup(self.bot.close)
        self.bot.llm_enabled = True
        self.bot.parse_accounts = lambda: ["Expenses:Food", "Assets:Cash"]
        self.bot.get_telegram_file_bytes = lambda file_id: b"fakeimg"
        self.bot.send_draft_for_review = MagicMock()
        self.bot.send_message = MagicMock()
        self.bot.call_openai_vision_invest = MagicMock(
            return_value='2026-07-25 * "IBKR" "buy"\n  Assets:Cash  -1 USD\n  Expenses:Food  1 USD')
        self.bot.call_openai_vision_expense = MagicMock(
            return_value='2026-07-25 * "Cafe" "lunch"\n  Expenses:Food  1 CNY\n  Assets:Cash')

    def _msg(self, caption):
        return {"message": {"chat": {"id": 123}, "caption": caption,
                            "photo": [{"file_id": "f1", "file_size": 100}]}}

    def test_invest_caption_routes_to_invest(self):
        self.bot.handle_photo_message(self._msg("isa buy"))
        self.bot.call_openai_vision_invest.assert_called_once()
        self.bot.call_openai_vision_expense.assert_not_called()
        self.bot.send_draft_for_review.assert_called_once()

    def test_plain_caption_routes_to_expense(self):
        self.bot.handle_photo_message(self._msg("lunch"))
        self.bot.call_openai_vision_expense.assert_called_once()
        self.bot.call_openai_vision_invest.assert_not_called()

    def test_no_caption_routes_to_expense(self):
        self.bot.handle_photo_message(self._msg(""))
        self.bot.call_openai_vision_expense.assert_called_once()

    def test_download_failure_replies_and_makes_no_draft(self):
        self.bot.get_telegram_file_bytes = lambda file_id: None
        self.bot.handle_photo_message(self._msg("lunch"))
        self.bot.send_message.assert_called_once_with(123, "Failed to download the image.")
        self.bot.send_draft_for_review.assert_not_called()

    def test_vision_error_replies_failure_and_makes_no_draft(self):
        self.bot.call_openai_vision_expense = MagicMock(side_effect=RuntimeError("boom"))
        self.bot.handle_photo_message(self._msg("lunch"))
        self.bot.send_draft_for_review.assert_not_called()
        self.assertTrue(any("Failed to process screenshot" in str(c.args)
                            for c in self.bot.send_message.call_args_list))


class TestUndoConfirmCallback(unittest.TestCase):
    """undo_confirm rewrites the whole file, so its commit branch handles 409/422 specially
    (no retry — the precomputed content is stale) vs other errors (restore for one-tap retry)."""

    def setUp(self):
        self.bot = make_bot()
        self.addCleanup(self.bot.close)
        self.bot.answer_callback_query = MagicMock()
        self.bot.send_message = MagicMock()
        self.bot.edit_message_reply_markup = MagicMock()
        self.pid = self.bot.next_pending_id()
        self.bot.pending_llm_entries[self.pid] = {
            "kind": "undo",
            "chat_id": 123,
            "transaction_text": '2026-07-01 * "X" "y"',
            "new_content": "; ledger\n",
            "file_sha": "sha0",
            "commit_message": "Revert: X y",
            "created_at": time.time(),
        }

    def _fire(self):
        self.bot.handle_callback_query({"callback_query": {
            "id": "cb", "data": f"undo_confirm:{self.pid}",
            "message": {"chat": {"id": 123}, "message_id": 7}}})

    def test_success_commits_and_consumes_pending(self):
        self.bot._github_put_file = MagicMock(return_value=(True, 200))
        self._fire()
        self.bot._github_put_file.assert_called_once()
        self.assertNotIn(self.pid, self.bot.pending_llm_entries)
        self.assertTrue(any("已撤回" in str(c.args) for c in self.bot.send_message.call_args_list))

    def test_conflict_does_not_restore_and_tells_user_to_rerun(self):
        self.bot._github_put_file = MagicMock(return_value=(False, 409))
        self._fire()
        # Stale precomputed content: the claimed entry stays gone, user must re-run /undo.
        self.assertNotIn(self.pid, self.bot.pending_llm_entries)
        self.assertTrue(any("/undo" in str(c.args) for c in self.bot.send_message.call_args_list))

    def test_other_error_restores_for_retry(self):
        self.bot._github_put_file = MagicMock(return_value=(False, 500))
        self._fire()
        # A transient failure keeps the draft (fresh) so one more tap retries.
        self.assertIn(self.pid, self.bot.pending_llm_entries)


class TestCallbackQueryBranches(unittest.TestCase):
    """Non-happy-path routing in handle_callback_query."""

    def setUp(self):
        self.bot = make_bot()
        self.addCleanup(self.bot.close)
        self.bot.answer_callback_query = MagicMock()
        self.bot.send_message = MagicMock()
        self.bot.edit_message_reply_markup = MagicMock()

    def test_malformed_data_is_unknown_action(self):
        # No colon → cannot split into (action, pending_id).
        self.bot.pending_llm_entries["keep"] = {"chat_id": 123, "created_at": time.time()}
        self.bot.handle_callback_query({"callback_query": {
            "id": "cb", "data": "garbage",
            "message": {"chat": {"id": 123}, "message_id": 1}}})
        self.bot.answer_callback_query.assert_called_once_with("cb", "Unknown action")
        self.assertIn("keep", self.bot.pending_llm_entries)  # untouched

    def test_decline_reason_binds_chat_and_keeps_draft(self):
        pid = self.bot.next_pending_id()
        self.bot.pending_llm_entries[pid] = self.bot._make_pending_entry(123, "e", "m", "u", "2026-07-01")
        self.bot.handle_callback_query({"callback_query": {
            "id": "cb", "data": f"decline_reason:{pid}",
            "message": {"chat": {"id": 123}, "message_id": 1}}})
        # Recheck flow is armed: chat is bound to the pending id and the draft is NOT popped.
        self.assertEqual(self.bot.pending_decline_reasons.get(123), pid)
        self.assertIn(pid, self.bot.pending_llm_entries)


class TestSendMessageHardening(unittest.TestCase):
    """R1/R2: over-limit plain text is truncated; a non-JSON error body can't raise."""

    def setUp(self):
        self.bot = make_bot()
        self.addCleanup(self.bot.close)

    def _resp(self, status=200, json_ok=True, text="ok"):
        r = MagicMock()
        r.status_code = status
        r.text = text
        if json_ok:
            r.json.return_value = {"ok": True}
        else:
            r.json.side_effect = ValueError("no json")
        return r

    def test_long_plain_text_truncated(self):
        captured = {}
        def fake_post(url, json=None, timeout=None):
            captured["text"] = json["text"]
            return self._resp()
        with patch.object(main.HTTP, "post", side_effect=fake_post):
            self.bot.send_message(123, "x" * 5000)
        self.assertLessEqual(len(captured["text"]), main.TELEGRAM_MESSAGE_LIMIT)
        self.assertTrue(captured["text"].endswith("…"))

    def test_html_text_not_truncated(self):
        # HTML callers pre-truncate their payload; send_message must not blind-cut a tag.
        captured = {}
        def fake_post(url, json=None, timeout=None):
            captured["text"] = json["text"]
            return self._resp()
        body = "<pre>" + "y" * 5000 + "</pre>"
        with patch.object(main.HTTP, "post", side_effect=fake_post):
            self.bot.send_message(123, body, parse_mode="HTML")
        self.assertEqual(captured["text"], body)

    def test_non_json_error_body_does_not_raise(self):
        resp = self._resp(status=502, json_ok=False, text="<html>bad gateway</html>")
        with patch.object(main.HTTP, "post", return_value=resp):
            self.assertEqual(self.bot.send_message(123, "hi"), {})


class TestScrub(unittest.TestCase):
    """S1: control chars stripped from untrusted text before it reaches the terminal."""

    def test_strips_ansi_and_newlines(self):
        self.assertEqual(main._scrub("a\x1b[31mred\nnext\r\x00"), "a?[31mred?next??")

    def test_non_str_coerced(self):
        self.assertEqual(main._scrub({"k": "v"}), str({"k": "v"}))


class TestCappedCodeBlock(unittest.TestCase):
    """R3: the HTML code block stays within budget even after escaping expands the text."""

    def test_short_text_untouched(self):
        block, truncated = main._capped_code_block("hello", 4096)
        self.assertEqual(block, "<pre><code>hello</code></pre>")
        self.assertFalse(truncated)

    def test_escaping_expansion_respected(self):
        # 3000 double-quotes each escape to &quot; (6x); the wrapped block must still fit.
        block, truncated = main._capped_code_block('"' * 3000, 500)
        self.assertLessEqual(len(block), 500)
        self.assertTrue(truncated)

    def test_empty_stays_empty_block(self):
        block, truncated = main._capped_code_block("", 4096)
        self.assertEqual(block, "<pre><code></code></pre>")
        self.assertFalse(truncated)

    def test_astral_emoji_counted_as_utf16(self):
        # Telegram counts an astral emoji as 2 UTF-16 units; a budget measured in code points
        # would let an all-emoji block slip over the real cap. The returned block must fit the
        # budget in UTF-16 units, not code points.
        block, truncated = main._capped_code_block("🎉" * 2000, 500)
        self.assertLessEqual(main._utf16_len(block), 500)
        self.assertTrue(truncated)


class TestUtf16Len(unittest.TestCase):
    def test_astral_and_bmp(self):
        self.assertEqual(main._utf16_len("今"), 1)   # BMP CJK
        self.assertEqual(main._utf16_len("🎉"), 2)   # astral emoji = surrogate pair
        self.assertEqual(main._utf16_len("ab"), 2)


class TestGetTelegramFileBytes(unittest.TestCase):
    """R4: a 200 with a malformed body honours the None contract instead of raising."""

    def setUp(self):
        self.bot = make_bot()
        self.addCleanup(self.bot.close)

    def test_missing_result_key_returns_none(self):
        r = MagicMock(); r.status_code = 200; r.json.return_value = {"ok": True}
        with patch.object(main.HTTP, "get", return_value=r):
            self.assertIsNone(self.bot.get_telegram_file_bytes("f1"))

    def test_non_json_body_returns_none(self):
        r = MagicMock(); r.status_code = 200; r.json.side_effect = ValueError("no json")
        with patch.object(main.HTTP, "get", return_value=r):
            self.assertIsNone(self.bot.get_telegram_file_bytes("f1"))


class TestManualTransactionValidation(unittest.TestCase):
    """S2: the hand-entered multi-line path escapes quotes and parses before committing."""

    def setUp(self):
        self.bot = make_bot()
        self.addCleanup(self.bot.close)
        self.bot.send_message = MagicMock()
        self.appended = {}
        def fake_append(appendix, msg, path):
            self.appended["appendix"] = appendix
            return True, ""
        self.bot.append_to_file = fake_append
        self.bot.match_account = lambda suffix: {
            "coffee": "Expenses:Food:Coffee", "cash": "Assets:Cash"}.get(suffix.lower())

    def _send(self, text):
        self.bot.handle_message({"message": {"text": text, "chat": {"id": 123}}})

    def test_quote_in_payee_is_escaped_and_parses(self):
        self._send('示例乙"VIP"\n咖啡\ncoffee 30 CNY\ncash -30 CNY')
        appendix = self.appended.get("appendix", "")
        self.assertIn('\\"VIP\\"', appendix)
        import beancount.parser.parser as parser
        _e, errors, _o = parser.parse_string(appendix)
        self.assertEqual(errors, [])

    def test_backslash_in_payee_round_trips(self):
        # A literal backslash must be escaped so beancount stores it faithfully rather than
        # interpreting \U / \n as an escape and silently mangling the payee.
        self._send('C:\\Users\\me\n备注\ncoffee 30 CNY\ncash -30 CNY')
        appendix = self.appended.get("appendix", "")
        import beancount.parser.parser as parser
        entries, errors, _o = parser.parse_string(appendix)
        self.assertEqual(errors, [])
        self.assertEqual(entries[0].payee, 'C:\\Users\\me')

    def test_leading_digit_currency_rejected(self):
        # "3NVD" parses as a number in beancount; reject it with a clear message up front.
        self.bot.send_message.reset_mock()
        self._send('店\n备注\ncoffee 30 3NVD\ncash -30 3NVD')
        self.assertNotIn("appendix", self.appended)
        self.assertTrue(any("货币符号" in str(c.args)
                            for c in self.bot.send_message.call_args_list))

    def test_single_letter_currency_rejected(self):
        # beancount requires >= 2 chars; a single letter must be rejected up front rather
        # than passing the regex and hitting an opaque parser error at commit.
        self.bot.send_message.reset_mock()
        self._send('店\n备注\ncoffee 30 X\ncash -30 X')
        self.assertNotIn("appendix", self.appended)
        self.assertTrue(any("货币符号" in str(c.args)
                            for c in self.bot.send_message.call_args_list))

    def test_letters_then_digits_currency_accepted(self):
        self._send('店\n备注\ncoffee 30 NVD3\ncash -30 NVD3')
        appendix = self.appended.get("appendix", "")
        import beancount.parser.parser as parser
        _e, errors, _o = parser.parse_string(appendix)
        self.assertEqual(errors, [])


class TestQueryColumnCompatibility(unittest.TestCase):
    def test_legacy_and_dbapi_columns_render_identically(self):
        rows = [("2000-01-02", "Example Shop")]
        legacy = [("date", str), ("payee", str)]
        dbapi = [("date", 1, None, None, None, None, None), ("payee", 2, None, None, None, None, None)]
        self.assertEqual(main.format_query_result(legacy, rows), main.format_query_result(dbapi, rows))


class TestNormalizeAndValidateLLMEntry(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()
        self.addCleanup(self.bot.close)
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
        self.addCleanup(self.bot.close)
        self.bot.send_message = MagicMock()
        self.bot._spawn_handler = lambda fn, update, chat_id: fn(update)

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
        self.addCleanup(self.bot.close)

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
        self.addCleanup(self.bot.close)
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
        self.addCleanup(self.bot.close)
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
        self.addCleanup(bot.close)
        bot.send_message = MagicMock()
        self._spawn_and_wait(bot, 123)
        self.assertEqual(bot.send_message.call_count, 1)

    def test_unauthorized_user_gets_nothing(self):
        bot = make_bot()
        self.addCleanup(bot.close)
        bot.send_message = MagicMock()
        self._spawn_and_wait(bot, 999)
        self.assertEqual(bot.send_message.call_count, 0)

    def test_failure_to_notify_does_not_raise(self):
        bot = make_bot()
        self.addCleanup(bot.close)
        bot.send_message = MagicMock(side_effect=RuntimeError("telegram down"))
        self._spawn_and_wait(bot, 123)


class TestAuthorizationGate(unittest.TestCase):
    """CHAT_ID is default-deny: empty means nobody, not everybody."""

    def test_empty_chat_id_refuses_to_start(self):
        with patch.dict(MOCK_CONFIG, {"CHAT_ID": ""}):
            with self.assertRaises(ValueError):
                make_bot()

    def test_is_authorized_matches_whitelist(self):
        with patch.dict(MOCK_CONFIG, {"CHAT_ID": "123,456"}):
            bot = make_bot()
            self.addCleanup(bot.close)
            self.addCleanup(bot.close)
            self.assertTrue(bot.is_authorized(123))
            self.assertTrue(bot.is_authorized("456"))
            self.assertFalse(bot.is_authorized(999))
            self.assertFalse(bot.is_authorized(None))

    def test_unauthorized_message_is_dropped_silently(self):
        with patch.dict(MOCK_CONFIG, {"CHAT_ID": "123"}):
            bot = make_bot()
            self.addCleanup(bot.close)
            bot.send_message = MagicMock()
            bot.parse_accounts = MagicMock(side_effect=AssertionError("must not reach GitHub"))
            bot.handle_message({"message": {"text": "/last 50", "chat": {"id": 999}}})
            self.assertEqual(bot.send_message.call_count, 0)

    def test_unauthorized_callback_cannot_claim_a_pending_entry(self):
        with patch.dict(MOCK_CONFIG, {"CHAT_ID": "123"}):
            bot = make_bot()
            self.addCleanup(bot.close)
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
        self.addCleanup(self.bot.close)
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
        self.addCleanup(self.bot.close)
        self.bot.send_message = MagicMock()
        self.bot.answer_callback_query = MagicMock()
        self.bot.edit_message_reply_markup = MagicMock()
        self.gh = FakeGitHub()
        self.bot.github_download_file = self.gh.download
        self.bot._github_put_file = self.gh.put
        self.bot._download_ledger_snapshot = lambda: (self.gh.sha, {MOCK_CONFIG["FILE_PATH"]: self.gh.download()})
        self.bot._list_bean_files = lambda: (self.gh.sha, {})
        self.bot.parse_accounts = lambda: ["Assets:Cash"]
        self.bot.review_journal = MagicMock()
        validation = patch("beancounter.drafts.check_ledger")
        validation.start()
        self.addCleanup(validation.stop)

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
        self.assertIn("没有找到", main.format_query_result([("date", str)], []))

    def test_columns_sized_by_display_width(self):
        # date -> 日期 (width 4), narration -> 摘要 (width 4); data widens them.
        rtypes = [("date", str), ("narration", str)]
        rows = [("2026-07-02", "买菜和日用品"), ("2026-07-01", "咖啡")]
        lines = main.format_query_result(rtypes, rows).splitlines()
        widths = [len(s) for s in lines[1].split("  ")]
        self.assertEqual(widths, [10, 12])  # 12 = display width of 买菜和日用品, not 6

    def test_headers_are_localized(self):
        rtypes = [("date", str), ("payee", str), ("narration", str), ("position", str)]
        header = main.format_query_result(rtypes, [("d", "p", "n", "x")]).splitlines()[0]
        self.assertIn("日期", header)
        self.assertIn("商家", header)
        self.assertIn("摘要", header)
        self.assertIn("金额", header)
        for raw in ("date", "payee", "narration", "position"):
            self.assertNotIn(raw, header)

    def test_aggregate_headers_localized(self):
        # sum_* / count_* have no AS alias from the LLM.
        self.assertEqual(main._friendly_header("sum_position"), "合计")
        self.assertEqual(main._friendly_header("sum_number"), "合计")
        self.assertEqual(main._friendly_header("count_position"), "笔数")
        self.assertEqual(main._friendly_header("month"), "月")
        self.assertEqual(main._friendly_header("monthly_avg"), "月均")
        self.assertEqual(main._friendly_header("some_alias"), "some_alias")  # unknown kept

    def test_decimal_rounded_and_grouped(self):
        from decimal import Decimal
        out = main.format_query_result(
            [("total", str), ("monthly_avg", str)],
            [(Decimal("1234.5"), Decimal("21.66666666666666666666666667"))])
        self.assertIn("1,234.50", out)
        self.assertIn("21.67", out)
        self.assertNotIn("21.6666", out)

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
    "1970-01-01 open Assets:Bank:SampleBank:Current GBP\n"
    "1970-01-01 open Liabilities:CreditCard:DemoBank GBP\n"
)
LEDGER_JOURNAL = '''
2026-06-20 * "ExampleShop" "上月买菜"
  Expenses:Food           30.00 GBP
  Liabilities:CreditCard:DemoBank

2026-07-01 * "ExampleCafe" "咖啡"
  Expenses:Food            3.50 GBP
  Liabilities:CreditCard:DemoBank

2026-07-05 * "ExampleBooks" "书"
  Expenses:Education      15.00 GBP
  Assets:Bank:SampleBank:Current
'''


# The main file pulls accounts in via an include glob, like the real ledger — load_ledger
# must mirror every file to disk and let beancount resolve the include, not concatenate.
LEDGER_MAIN = 'include "accounts/*.bean"\n' + LEDGER_JOURNAL
LEDGER_TREE = {p: p for p in ["test.bean", "accounts/assets.bean", "accounts/empty.bean"]}


def _ledger_download(file_path="test.bean"):
    if file_path == "accounts/assets.bean":
        return {"content": LEDGER_ACCOUNTS, "sha": "s"}
    if file_path.startswith("accounts/"):
        return {"content": "", "sha": "s"}
    return {"content": LEDGER_MAIN, "sha": "s"}


class TestListBeanFiles(unittest.TestCase):
    """_list_bean_files parses the trees API into (tree_sha, paths). Mocks the HTTP
    response, not the method, so the real return shape is exercised — a tuple/list
    mismatch would crash load_ledger's unpack in production while method-mocking
    tests stayed green."""

    def setUp(self):
        self.bot = make_bot()
        self.addCleanup(self.bot.close)

    def test_returns_sha_and_bean_paths_only(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"sha": "tree123", "truncated": False, "tree": [
            {"type": "blob", "path": "test.bean", "sha": "s1"},
            {"type": "blob", "path": "accounts/assets.bean", "sha": "s2"},
            {"type": "blob", "path": "README.md"},
            {"type": "tree", "path": "accounts"},
        ]}
        with patch.object(main.HTTP, "get", return_value=resp):
            result = self.bot._list_bean_files()
        self.assertEqual(result, ("tree123", {"test.bean": "s1", "accounts/assets.bean": "s2"}))

    def test_none_on_http_error(self):
        with patch.object(main.HTTP, "get", return_value=MagicMock(status_code=500)):
            self.assertIsNone(self.bot._list_bean_files())


class TestLedgerQuery(unittest.TestCase):
    """load_ledger mirrors the repo to a temp dir and load_file()s it, so include
    directives (globs included) resolve — concatenation could not do that."""

    def setUp(self):
        self.bot = make_bot()
        self.addCleanup(self.bot.close)
        self.bot._list_bean_files = lambda: ("sha1", dict(LEDGER_TREE))
        self.bot._download_blob = _ledger_download

    def test_load_ledger_parses(self):
        entries, options_map = self.bot.load_ledger()
        self.assertTrue(entries)
        self.assertIn("dcontext", options_map)

    def test_include_glob_is_resolved(self):
        # The account opens live in an included file; without resolving the include the
        # journal's postings would be against undeclared accounts and error out.
        _, rrows = self.bot.run_bql('SELECT count(date) WHERE account ~ "DemoBank"')
        self.assertEqual(list(rrows[0])[0], 2)

    def test_rejects_when_tree_unavailable(self):
        self.bot._list_bean_files = lambda: None
        with self.assertRaises(ValueError):
            self.bot.load_ledger()

    def test_load_ledger_returns_none_without_journal(self):
        self.bot._download_blob = lambda p="x": {"content": "", "sha": ""}
        self.assertIsNone(self.bot.load_ledger())

    def test_same_tree_sha_is_served_from_cache(self):
        # First load pays for downloads; a second load with the same tree sha must not.
        dl = MagicMock(side_effect=_ledger_download)
        self.bot._download_blob = dl
        self.bot.load_ledger()
        first = dl.call_count
        self.assertGreater(first, 0)
        self.bot.load_ledger()
        self.assertEqual(dl.call_count, first)  # no new downloads

    def test_changed_tree_sha_reloads(self):
        dl = MagicMock(side_effect=_ledger_download)
        self.bot._download_blob = dl
        self.bot._list_bean_files = lambda: ("sha1", dict(LEDGER_TREE))
        self.bot.load_ledger()
        first = dl.call_count
        self.bot._list_bean_files = lambda: ("sha2", dict(LEDGER_TREE))  # ledger changed
        self.bot.load_ledger()
        self.assertGreater(dl.call_count, first)  # re-downloaded on new sha

    def test_unavailable_tree_does_not_download_or_cache(self):
        self.bot._list_bean_files = lambda: None
        dl = MagicMock(side_effect=_ledger_download)
        self.bot._download_blob = dl
        with self.assertRaises(ValueError):
            self.bot.load_ledger()
        dl.assert_not_called()
        self.assertIsNone(self.bot._ledger_cache["tree_sha"])

    def test_run_bql_filters(self):
        _, rrows = self.bot.run_bql('SELECT date WHERE account ~ "DemoBank" ORDER BY date DESC')
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
        self.addCleanup(b.close)
        b._accounts_for_prompt = lambda: ["Expenses:Food (GBP)"]
        b._call_llm_backends = MagicMock(side_effect=list(replies))
        return b

    def test_query(self):
        b = self._bot('{"intent": "query", "bql": "SELECT date"}')
        self.addCleanup(b.close)
        self.assertEqual(b.route_intent("最近10条", "2026-07-17")["intent"], "query")

    def test_entry(self):
        b = self._bot('{"intent": "entry"}')
        self.addCleanup(b.close)
        self.assertEqual(b.route_intent("示例乙 35", "2026-07-17")["intent"], "entry")

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
        self.addCleanup(b.close)
        b._list_bean_files = lambda: ("sha1", dict(LEDGER_TREE))
        b._download_blob = _ledger_download
        b._accounts_for_prompt = lambda: ["Expenses:Food (GBP)"]
        b._call_llm_backends = MagicMock(side_effect=list(replies))
        return b

    def test_good_bql_runs_directly(self):
        b = self._bot()
        self.addCleanup(b.close)
        _, out = b.answer_query("q", 'SELECT date WHERE account ~ "DemoBank"', "2026-07-17")
        self.assertIn("2026-07", out)
        self.assertEqual(b._call_llm_backends.call_count, 0)

    def test_bad_bql_is_repaired(self):
        b = self._bot('{"intent":"query","bql":"SELECT date WHERE account ~ \\"DemoBank\\""}')
        self.addCleanup(b.close)
        bql, out = b.answer_query("q", "SELECT bogus", "2026-07-17")
        self.assertIn("2026-07", out)
        self.assertNotIn("bogus", bql)

    def test_retries_bounded(self):
        b = self._bot(*(['{"intent":"query","bql":"SELECT still_bad"}'] * 5))
        self.addCleanup(b.close)
        with self.assertRaises(ValueError):
            b.answer_query("q", "SELECT bad", "2026-07-17")
        self.assertLessEqual(b._call_llm_backends.call_count, main.MAX_BEANCOUNT_RETRIES)

    def test_download_failure_not_retried_at_llm(self):
        b = self._bot()
        self.addCleanup(b.close)
        b._download_blob = MagicMock(side_effect=ValueError("download failed"))
        with self.assertRaises(ValueError):
            b.answer_query("q", "SELECT date", "2026-07-17")
        self.assertEqual(b._call_llm_backends.call_count, 0)


class TestQueryEndToEnd(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()
        self.addCleanup(self.bot.close)
        self.bot.llm_enabled = True
        self.bot.send_message = MagicMock()
        self.bot._list_bean_files = lambda: ("sha1", dict(LEDGER_TREE))
        self.bot._download_blob = _ledger_download
        self.bot.parse_accounts = lambda: ["Expenses:Food", "Liabilities:CreditCard:DemoBank"]
        self.bot._accounts_for_prompt = lambda: ["Expenses:Food (GBP)"]

    def test_query_answers_without_a_draft(self):
        self.bot._call_llm_backends = MagicMock(return_value=(
            '{"intent": "query", "bql": "SELECT date, payee, position '
            'WHERE account ~ \\"DemoBank\\" ORDER BY date DESC LIMIT 10"}'))
        self.bot.handle_message({"message": {"text": "列出最近的 demobank 记录", "chat": {"id": 123}}})
        sent = "\n".join(str(c) for c in self.bot.send_message.call_args_list)
        self.assertIn("ExampleShop", sent)          # the result reaches the user
        self.assertNotIn("SELECT", sent)      # the raw BQL does not
        self.assertEqual(len(self.bot.pending_llm_entries), 0)

    def test_entry_still_creates_a_draft(self):
        self.bot._call_llm_backends = MagicMock(return_value='{"intent": "entry"}')
        self.bot.call_openai_compatible = MagicMock(
            return_value='2026-07-17 * "S" "咖啡"\n  Expenses:Food  3.50 GBP\n  Liabilities:CreditCard:DemoBank')
        self.bot.add_non_pnl_accounts_to_commit_message = lambda m, a: m
        self.bot.build_review_buttons = lambda p: {}
        self.bot.handle_message({"message": {"text": "示例乙 3.5", "chat": {"id": 123}}})
        self.assertEqual(len(self.bot.pending_llm_entries), 1)


class TestDateParsing(unittest.TestCase):
    """Bug 3: custom date must extract only YYYY-MM-DD, not trailing text."""

    def setUp(self):
        self.bot = make_bot()
        self.addCleanup(self.bot.close)
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
        self.addCleanup(bot.close)
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
        self.addCleanup(self.bot.close)

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


class TestCheckedApproval(unittest.TestCase):
    """Exercise real bean-check and the review gateway, mocking external I/O only."""

    def setUp(self):
        self.bot = make_bot()
        self.addCleanup(self.bot.close)
        self.bot.llm_enabled = True
        self.bot.send_message = MagicMock(return_value={"ok": True, "result": {"message_id": 7}})
        self.bot.edit_message_reply_markup = MagicMock()
        self.bot.answer_callback_query = MagicMock()
        self.bot._spawn_handler = lambda fn, update, chat_id: fn(update)
        self.gh = FakeGitHub()
        self.gh.content = 'include "accounts.bean"\n'
        self.accounts = "2020-01-01 open Assets:Cash GBP\n2020-01-01 open Expenses:Food GBP\n"
        self.bot._download_ledger_snapshot = lambda: (self.gh.sha, {
            MOCK_CONFIG["FILE_PATH"]: self.gh.download(), "accounts.bean": {"content": self.accounts, "sha": "accounts"}})
        self.bot._list_bean_files = lambda: (self.gh.sha, {})
        self.bot._github_put_file = self.gh.put
        self.bot.parse_accounts = lambda: ["Assets:Cash", "Expenses:Food"]
        self.bot._accounts_for_prompt = self.bot.parse_accounts
        self.bot._call_llm_backends = MagicMock(return_value='{"approved":true,"reason":"金额、账户一致"}')
        self.entry = '2026-07-01 * "Coffee" "咖啡"\n  Assets:Cash -5 GBP\n  Expenses:Food 5 GBP'
        self.pending = self.bot._make_pending_entry(123, self.entry, "entry", "现金买咖啡 5 GBP", "2026-07-01")
        self.pending["auto_confirm"] = True  # Simulate a delivered review message.
        self.bot.pending_llm_entries["1"] = self.pending

    def approve(self, action="approve"):
        self.bot.handle_callback_query({"callback_query": {
            "id": "cb", "data": f"{action}:1",
            "message": {"chat": {"id": 123}, "message_id": 7}}})

    def test_manual_confirmation_checks_and_commits(self):
        self.approve()
        self.assertEqual(len(self.gh.commits), 1)
        self.assertNotIn("1", self.bot.pending_llm_entries)
        self.bot._call_llm_backends.assert_called_once()

    def test_relative_date_review_distinguishes_save_timestamp(self):
        import json
        from datetime import datetime
        from beancounter.bot_utils import parse_natural_date

        original = "yesterday\nExample Shop refund 5 GBP cash"
        date_str, custom, _ = parse_natural_date(original, datetime(2026, 7, 2, 12))
        self.assertTrue(custom)
        self.assertEqual(date_str, "2026-07-01")
        self.pending.update(user_input=original, date_str=date_str)
        self.pending["appendix"] = (
            '2026-07-01 * "Example Shop" "Refund"\n'
            '  Assets:Cash 5 GBP\n  Expenses:Food -5 GBP'
        )
        stamp = "2026-07-02T12:00:00+00:00"
        with patch("beancounter.drafts.datetime") as clock:
            clock.now.return_value.isoformat.return_value = stamp
            self.approve()
        payload = self.bot._call_llm_backends.call_args.args[0]
        context = json.loads(payload["messages"][1]["content"][0]["text"])
        self.assertEqual(context["original_input"], original)
        self.assertEqual(context["resolved_date"], date_str)
        self.assertEqual(context["system_generated_datetime_metadata"], [f'datetime: "{stamp}"'])
        self.assertTrue(context["journal"].startswith(date_str))
        self.assertIn("禁止再次加减天数", payload["messages"][0]["content"])
        self.assertIn("不是交易发生时间", payload["messages"][0]["content"])
        self.assertEqual(len(self.gh.commits), 1)

        # Retrying a persisted payload must preserve the same provenance.
        self.bot.review_journal(self.pending, self.pending["commit_appendix"])
        retry = self.bot._call_llm_backends.call_args.args[0]
        self.assertEqual(json.loads(retry["messages"][1]["content"][0]["text"]), context)

    def test_existing_transaction_datetime_remains_subject_to_review(self):
        import json
        stamp = '  datetime: "2026-07-01T09:00:00+00:00"'
        self.pending["appendix"] = self.entry.replace('\n', '\n' + stamp + '\n', 1)
        self.approve()
        payload = self.bot._call_llm_backends.call_args.args[0]
        context = json.loads(payload["messages"][1]["content"][0]["text"])
        self.assertEqual(context["system_generated_datetime_metadata"], [])
        self.assertIn(stamp, context["journal"])
        self.assertEqual(len(self.gh.commits), 1)

    def test_timeout_uses_same_checks(self):
        self.pending["created_at"] = 0
        self.bot.cleanup_expired_drafts()
        self.assertEqual(len(self.gh.commits), 1)
        self.assertIn("超时自动确认", self.bot.send_message.call_args.args[1])

    def test_unbalanced_entry_is_blocked_before_review(self):
        self.pending["appendix"] = self.entry.replace("Food 5", "Food 6")
        self.approve()
        self.assertEqual(self.gh.commits, [])
        self.bot._call_llm_backends.assert_not_called()
        self.assertFalse(self.bot.pending_llm_entries["1"]["auto_confirm"])

    def test_unknown_account_blocked_by_full_ledger_check(self):
        self.pending["appendix"] = self.entry.replace("Assets:Cash", "Assets:Unknown")
        self.approve()
        self.assertEqual(self.gh.commits, [])
        self.assertIn("bean-check failed", self.bot.send_message.call_args.args[1])

    def test_existing_ledger_errors_block_commit(self):
        self.accounts += "2026-06-01 close Assets:Cash\n"
        self.approve()
        self.assertEqual(self.gh.commits, [])

    def test_rejected_uncertain_or_malformed_review_never_commits(self):
        for raw in ['{"approved":false,"reason":"币种错误"}', '{"approved":"true","reason":"ok"}',
                    'not json', '{"approved":true}', '[]']:
            with self.subTest(raw=raw):
                self.bot._call_llm_backends.return_value = raw
                self.approve()
                self.assertIn("1", self.bot.pending_llm_entries)
                self.assertEqual(self.gh.commits, [])

    def test_timeout_failure_does_not_auto_retry(self):
        self.pending["created_at"] = 0
        self.bot._call_llm_backends.side_effect = TimeoutError("LLM unavailable")
        self.bot.cleanup_expired_drafts()
        self.bot.pending_llm_entries["1"]["created_at"] = 0
        self.bot.cleanup_expired_drafts()
        self.bot._call_llm_backends.assert_called_once()
        self.assertEqual(self.gh.commits, [])

    def test_lost_put_response_reconciles_without_duplicate(self):
        def lost_response(*args):
            self.gh.put(*args)
            raise main.requests.Timeout("response lost")
        self.bot._github_put_file = lost_response
        self.approve()
        self.assertIn("1", self.bot.pending_llm_entries)
        self.bot._github_put_file = self.gh.put
        self.approve()
        self.assertNotIn("1", self.bot.pending_llm_entries)
        self.assertEqual(len(self.gh.commits), 1)
        self.assertEqual(self.gh.content.count('"Coffee"'), 1)

    def test_conflict_rechecks_new_snapshot(self):
        original = self.gh.put
        def conflict(*args):
            self.bot._github_put_file = original
            self.accounts += "2026-06-01 close Assets:Cash\n"
            self.gh.sha = "changed"
            return False, 409
        self.bot._github_put_file = conflict
        self.approve()
        self.assertEqual(self.gh.commits, [])
        self.assertIn("bean-check failed", self.bot.send_message.call_args.args[1])

    def test_feedback_pauses_timeout(self):
        self.approve("decline_reason")
        self.pending["created_at"] = 0
        self.bot.cleanup_expired_drafts()
        self.assertIn("1", self.bot.pending_llm_entries)
        self.assertEqual(self.gh.commits, [])

    def test_manual_and_timeout_race_commits_once(self):
        self.pending["created_at"] = 0
        threads = [threading.Thread(target=self.approve),
                   threading.Thread(target=self.bot.cleanup_expired_drafts)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.gh.commits), 1)

    def test_undo_action_cannot_be_used_on_llm_draft(self):
        self.approve("undo_confirm")
        self.assertIn("1", self.bot.pending_llm_entries)
        self.assertEqual(self.gh.commits, [])

    def test_missing_photo_blocks_commit(self):
        self.pending["photo_file_id"] = "missing"
        self.bot.get_telegram_file_bytes = MagicMock(return_value=None)
        self.approve()
        self.bot._call_llm_backends.assert_not_called()
        self.assertEqual(self.gh.commits, [])

    def test_feedback_survives_regeneration(self):
        self.pending["photo_file_id"] = "photo"
        self.bot.call_openai_compatible = MagicMock(return_value=self.entry)
        self.bot.pending_llm_id = 1
        self.bot.run_recheck(123, "1", "实际用现金支付")
        replacement = self.bot.pending_llm_entries["2"]
        self.assertEqual(replacement["photo_file_id"], "photo")
        self.assertEqual(replacement["feedback"], ["实际用现金支付"])
        self.assertTrue(replacement["auto_confirm"])

    def test_failed_recheck_preserves_draft_with_timeout_paused(self):
        self.bot.call_openai_compatible = MagicMock(side_effect=ValueError("service unavailable"))
        self.bot.run_recheck(123, "1", "实际用现金支付")
        self.assertIn("1", self.bot.pending_llm_entries)
        self.assertFalse(self.pending["auto_confirm"])
        self.assertFalse(self.pending["rechecking"])
        self.assertEqual(self.pending["feedback"], ["实际用现金支付"])

    def test_discard_wins_before_timeout(self):
        self.approve("discard")
        self.bot.cleanup_expired_drafts()
        self.assertEqual(self.gh.commits, [])

    def test_photo_and_feedback_reach_reviewer(self):
        self.pending.update(photo_file_id="photo", feedback=["实际是 5 GBP"])
        self.bot.get_telegram_file_bytes = MagicMock(return_value=b"photo")
        self.approve()
        payload = self.bot._call_llm_backends.call_args.args[0]
        content = payload["messages"][-1]["content"]
        self.assertIn("实际是 5 GBP", content[0]["text"])
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(self.bot._call_llm_backends.call_args.kwargs["vision"])

    def test_failed_delivery_disarms_timeout(self):
        self.bot.send_message.return_value = {"ok": False}
        self.bot.send_draft_for_review(123, "draft", self.entry, "1")
        self.pending["created_at"] = 0
        self.bot.cleanup_expired_drafts()
        self.assertEqual(self.gh.commits, [])

    def test_successful_delivery_arms_timeout(self):
        self.pending["auto_confirm"] = False
        self.bot.send_draft_for_review(123, "draft", self.entry, "1")
        self.assertTrue(self.pending["auto_confirm"])
        self.assertEqual(self.pending["message_id"], 7)


class TestCustomPrompt(unittest.TestCase):
    def test_missing_personal_prompt_does_not_load_template(self):
        bot = make_bot()
        self.addCleanup(bot.close)
        payload = {"messages": [{"role": "system", "content": "synthetic task"}]}
        backend = {"base_url": "https://example.invalid", "api_key": "test", "model": "test"}
        response = MagicMock()
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        with patch.object(main.Path, "exists", return_value=False), \
             patch.object(main.Path, "read_text") as read, \
             patch.object(bot.settings, "LLM_BACKENDS", [backend]), \
             patch.object(main.HTTP, "post", return_value=response) as post:
            bot._call_llm_backends(payload)
        read.assert_not_called()
        self.assertEqual(post.call_args.kwargs["json"]["messages"], payload["messages"])

    def test_reloaded_for_text_and_vision_without_mutating_payload(self):
        bot = make_bot()
        self.addCleanup(bot.close)
        payload = {"messages": [{"role": "system", "content": "task"}]}
        backend = {"base_url": "https://example.invalid", "api_key": "test", "model": "test"}
        response = MagicMock()
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        with patch.object(main.Path, "exists", return_value=True), \
             patch.object(main.Path, "read_text", side_effect=["偏好一<!--隐藏示例-->", "偏好二"]), \
             patch.object(bot.settings, "LLM_BACKENDS", [backend]), \
             patch.object(main.HTTP, "post", return_value=response) as post:
            bot._call_llm_backends(payload)
            bot._call_llm_backends(payload, vision=True)
        first, second = [call.kwargs["json"]["messages"][0]["content"] for call in post.call_args_list]
        self.assertIn("偏好一", first)
        self.assertNotIn("隐藏示例", first)
        self.assertIn("偏好二", second)
        self.assertEqual(len(payload["messages"]), 1)


class TestIncompleteLedger(unittest.TestCase):
    def test_partial_download_never_cached(self):
        bot = make_bot()
        self.addCleanup(bot.close)
        bot._list_bean_files = lambda: ("tree", {MOCK_CONFIG["FILE_PATH"]: "root", "accounts.bean": "account"})
        bot._download_blob = MagicMock(side_effect=ValueError("download failed"))
        with self.assertRaises(ValueError):
            bot.load_ledger()
        self.assertIsNone(bot._ledger_cache["tree_sha"])
        self.assertIsNone(bot._snapshot_cache[0])

    def test_truncated_tree_is_rejected(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"truncated": True}
        with patch.object(main.HTTP, "get", return_value=response), self.assertRaises(ValueError):
            make_bot()._list_bean_files()

    def test_ambiguous_account_requires_full_name(self):
        bot = make_bot()
        self.addCleanup(bot.close)
        bot.parse_accounts = lambda: ["Assets:Bank:A:Current", "Assets:Bank:B:Current"]
        with self.assertRaisesRegex(main.AccountMatchError, "Assets:Bank:B:Current"):
            bot.match_account("Current")
        self.assertEqual(bot.match_account("assets:bank:a:current"), "Assets:Bank:A:Current")


if __name__ == "__main__":
    unittest.main(verbosity=2)
