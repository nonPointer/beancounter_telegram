"""Real BQL and post-save snapshot reuse with synthetic journals and no network."""

import hashlib
import os
import sys
import time
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from beancounter.bot import Bot
from beancounter.ledger_validation import check_ledger
from beancounter.reports import _account_labels
from beancounter.bot_utils import _utf16_len
from test_bot import MOCK_CONFIG


ACCOUNTS = """2000-01-01 open Assets:Cash GBP,USD
2000-01-01 open Assets:Cash:Other GBP
2000-01-01 open Assets:Shares DEMO
2000-01-01 open Liabilities:Card GBP
2000-01-01 open Expenses:Food:Cafe GBP,USD
2000-01-01 open Expenses:Food:Groceries GBP
2000-01-01 open Expenses:Travel GBP
2000-01-01 open Equity:Opening GBP
"""
HISTORY = """2026-08-01 * "Opening"
  Assets:Cash 100 GBP
  Equity:Opening -100 GBP
2026-08-31 * "Previous month"
  Expenses:Food:Cafe 9 GBP
  Assets:Cash -9 GBP
2026-09-01 * "Food"
  Expenses:Food:Groceries 20 GBP
  Assets:Cash -20 GBP
2026-09-02 * "Refund"
  Expenses:Food:Cafe -2 GBP
  Assets:Cash 2 GBP
2026-09-03 * "Other currency"
  Expenses:Food:Cafe 3 USD
  Assets:Cash -3 USD
2026-09-04 * "Travel"
  Expenses:Travel 7 GBP
  Liabilities:Card -7 GBP
2026-09-05 * "Subaccount"
  Assets:Cash:Other 50 GBP
  Equity:Opening -50 GBP
2026-09-06 * "Shares"
  Assets:Shares 0.123456 DEMO {10 GBP}
  Assets:Cash -1.234560 GBP
2026-10-01 * "Future expense"
  Expenses:Travel 11 GBP
  Liabilities:Card -11 GBP
"""
ENTRY = '2026-09-10 * "Cafe <Demo>" "Lunch"\n  Expenses:Food:Cafe 5 GBP\n  Assets:Cash -5 GBP'


def blob(text):
    encoded = text.encode()
    return hashlib.sha1(f"blob {len(encoded)}\0".encode() + encoded).hexdigest()


class TestReports(unittest.TestCase):
    def setUp(self):
        self.bot = Bot(settings=MOCK_CONFIG, state_path=":memory:")
        self.addCleanup(self.bot.close)
        self.bot.send_message = MagicMock()
        self.bot.edit_message_reply_markup = MagicMock()
        self.bot.review_journal = MagicMock()
        self.bot.parse_accounts = MagicMock(return_value=["Assets:Cash", "Expenses:Food:Cafe"])
        self.texts = {"test.bean": 'include "accounts.bean"\n' + HISTORY, "accounts.bean": ACCOUNTS}
        self.tree = "initial"
        self.puts = 0
        self.bot._list_bean_files = MagicMock(side_effect=lambda: (self.tree, {p: blob(t) for p, t in self.texts.items()}))
        self.bot._download_blob = MagicMock(side_effect=lambda sha: {"sha": sha, "content": next(t for t in self.texts.values() if blob(t) == sha)})
        self.bot.github_download_file = lambda path=None: {"sha": blob(self.texts[path or "test.bean"]), "content": self.texts[path or "test.bean"]}
        self.bot._github_put_file = self.put

    def put(self, content, sha, message, file_path=None):
        path = file_path or "test.bean"
        if sha != blob(self.texts[path]):
            return False, 409
        self.texts[path] = content
        self.puts += 1
        self.tree = f"saved-{self.puts}"
        return True, 200

    def pending(self, appendix=ENTRY):
        return self.bot._make_pending_entry(123, appendix, "Save synthetic entry", "Lunch 5 GBP", "2026-09-10")

    def send_manual(self, currency="GBP"):
        self.bot.send_transaction_report = MagicMock()
        self.bot._call_llm_backends = MagicMock(side_effect=AssertionError("manual journal needs no LLM"))
        self.bot.handle_message({"message": {"chat": {"id": 123}, "text": (
            f"2026-09-10\nSynthetic Cafe\nManual purchase\n"
            f"Expenses:Food:Cafe 5 {currency}\nAssets:Cash -5 {currency}"
        )}})

    def test_manual_journal_checks_exact_full_candidate_before_writing(self):
        with patch("beancounter.drafts.check_ledger", wraps=check_ledger) as check:
            self.send_manual()
        self.assertEqual(self.puts, 1)
        check.assert_called_once()
        self.assertEqual(check.call_args.args[0], self.texts)
        self.assertIn("Manual purchase", self.texts["test.bean"])
        self.bot._call_llm_backends.assert_not_called()
        # The checked snapshot can also serve the subsequent report/query.
        with patch("beancounter.ledger.check_ledger", side_effect=AssertionError("must reuse exact texts")):
            entries, _ = self.bot.load_ledger()
        self.assertTrue(any(getattr(entry, "narration", None) == "Manual purchase" for entry in entries))

    def test_manual_journal_blocks_currency_disallowed_by_account(self):
        self.send_manual(currency="CNY")
        self.assertEqual(self.puts, 0)
        self.assertIn("账本校验未通过，尚未保存", self.bot.send_message.call_args.args[1])
        self.bot.send_transaction_report.assert_not_called()

    def test_manual_journal_blocks_closed_account_despite_cached_name_match(self):
        self.texts["accounts.bean"] += "2026-09-09 close Assets:Cash\n"
        self.send_manual()
        self.assertEqual(self.puts, 0)
        self.assertIn("bean-check failed", self.bot.send_message.call_args.args[1])

    def test_manual_journal_blocks_errors_elsewhere_in_complete_ledger(self):
        self.texts["accounts.bean"] += "2000-01-01 commodity invalid\n"
        self.send_manual()
        self.assertEqual(self.puts, 0)
        self.assertIn("accounts.bean", self.bot.send_message.call_args.args[1])

    def test_manual_journal_requires_root_to_include_target(self):
        self.bot.settings.LEDGER_ROOT = "root.bean"
        self.texts["root.bean"] = 'include "accounts.bean"\n'
        self.send_manual()
        self.assertEqual(self.puts, 0)
        self.assertIn("does not include journal", self.bot.send_message.call_args.args[1])

    def test_manual_journal_blocks_incomplete_download(self):
        self.bot._download_blob.side_effect = ConnectionError("synthetic download failure")
        self.send_manual()
        self.assertEqual(self.puts, 0)
        self.assertIn("无法获取完整账本，尚未保存", self.bot.send_message.call_args.args[1])

    def test_manual_journal_conflict_rebuilds_and_checks_again(self):
        attempts = []

        def conflicting_put(content, sha, message, file_path=None):
            attempts.append(content)
            if len(attempts) == 1:
                self.texts["test.bean"] += '\n2026-09-09 * "Concurrent entry"\n  Assets:Cash -1 GBP\n  Expenses:Food:Cafe 1 GBP\n'
                self.tree = "concurrent"
                return False, 409
            return self.put(content, sha, message, file_path)

        self.bot._github_put_file = conflicting_put
        with patch("beancounter.drafts.check_ledger", wraps=check_ledger) as check:
            self.send_manual()
        self.assertEqual(check.call_count, 2)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.puts, 1)
        self.assertIn("Concurrent entry", self.texts["test.bean"])
        self.assertEqual(self.texts["test.bean"].count("Manual purchase"), 1)

    def test_manual_journal_rechecks_other_files_changed_during_validation(self):
        listing = self.bot._list_bean_files.side_effect
        calls = []

        def changed_listing():
            calls.append(True)
            if len(calls) == 2:
                self.texts["accounts.bean"] += "2026-09-09 close Assets:Cash\n"
                self.tree = "closed-during-check"
            return listing()

        self.bot._list_bean_files.side_effect = changed_listing
        with patch("beancounter.drafts.check_ledger", wraps=check_ledger) as check:
            self.send_manual()
        self.assertEqual(check.call_count, 2)
        self.assertEqual(self.puts, 0)
        self.assertIn("账本校验未通过", self.bot.send_message.call_args.args[1])

    def test_manual_journal_conflict_cannot_bypass_a_new_validation_error(self):
        attempts = []

        def conflicting_put(*args):
            attempts.append(args)
            self.texts["accounts.bean"] += "2026-09-09 close Assets:Cash\n"
            self.tree = "closed-after-conflict"
            return False, 409

        self.bot._github_put_file = conflicting_put
        with patch("beancounter.drafts.check_ledger", wraps=check_ledger) as check:
            self.send_manual()
        self.assertEqual(check.call_count, 2)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.puts, 0)
        self.assertIn("账本校验未通过", self.bot.send_message.call_args.args[1])

    def test_manual_journal_requires_version_confirmation_after_validation(self):
        listed = self.bot._list_bean_files()
        self.bot._list_bean_files.side_effect = [listed, None]
        self.send_manual()
        self.assertEqual(self.puts, 0)
        self.assertIn("无法确认账本版本，尚未保存", self.bot.send_message.call_args.args[1])

    def test_manual_journal_replay_does_not_duplicate_a_successful_write(self):
        self.bot._handler_context.update_id = 1234
        self.send_manual()
        self.send_manual()
        self.assertEqual(self.puts, 1)
        self.assertEqual(self.texts["test.bean"].count("telegram-update: 1234"), 1)
        self.assertEqual(self.texts["test.bean"].count("Manual purchase"), 1)

    def test_month_categories_refunds_currencies_and_exact_account_balance(self):
        self.texts["test.bean"] += ENTRY + "\n"
        tables = self.bot.transaction_report_tables(ENTRY, today=date(2026, 9, 10))
        self.assertEqual(len(tables), 2)
        expenses, balances = tables[0][1], tables[1][1]
        self.assertIn("Food", expenses)
        self.assertNotIn("Expenses:", expenses)
        self.assertNotIn("Assets:", balances)
        self.assertNotIn("Groceries", expenses)
        self.assertIn("23 GBP, 3 USD", expenses)
        self.assertIn("30 GBP, 3 USD", expenses)
        self.assertIn("66.765440 GBP, -3 USD", balances)
        self.assertNotIn("Assets:Cash:Other", balances)
        self.assertNotIn("Liabilities:Card", balances)
        self.assertEqual(self.bot._list_bean_files.call_count, 1)

    def test_liability_sign_and_stock_precision(self):
        entry = '2026-09-10 * "Transfer"\n  Assets:Shares 0 DEMO\n  Liabilities:Card 0 GBP'
        tables = self.bot.transaction_report_tables(entry, today=date(2026, 9, 10))
        self.assertIn("-18 GBP", tables[1][1])
        self.assertIn("0.123456 DEMO", tables[1][1])

    def test_current_month_uses_bot_timezone_not_backdated_transaction(self):
        loaded = self.bot.load_ledger()
        clock = datetime(2026, 12, 31, 23, 59)
        with patch("beancounter.reports.datetime") as mocked:
            mocked.now.return_value = clock
            tables = self.bot.transaction_report_tables(ENTRY, loaded=loaded)
        mocked.now.assert_called_once_with(self.bot.timezone)
        self.assertIn("2026-12", tables[0][0])
        self.assertEqual(tables[0][1], "本月暂无开支。")

    def test_non_transaction_directive_does_not_load_ledger(self):
        self.assertEqual(self.bot.transaction_report_tables("2026-09-10 open Assets:New GBP"), [])
        self.bot._list_bean_files.assert_not_called()

    def test_zero_balance_and_no_asset_posting(self):
        self.texts["test.bean"] = 'include "accounts.bean"\n'
        tables = self.bot.transaction_report_tables(ENTRY, today=date(2026, 9, 10))
        self.assertIn("Cash", tables[1][1])
        self.assertIn("0", tables[1][1])
        entry = '2026-09-10 * "Reclassify"\n  Expenses:Travel 1 GBP\n  Expenses:Food:Cafe -1 GBP'
        self.assertIn("未涉及", self.bot.transaction_report_tables(entry)[1][1])

    def test_reviewed_save_reports_reuse_full_validation(self):
        with patch("beancounter.drafts.check_ledger", wraps=check_ledger) as commit_check, \
             patch("beancounter.ledger.check_ledger", wraps=check_ledger) as context_check:
            self.bot.approve_pending("1", self.pending())
        self.assertEqual(self.puts, 1)
        commit_check.assert_called_once()
        context_check.assert_not_called()
        self.assertEqual(self.bot.send_message.call_count, 3)
        self.assertIn("已保存", self.bot.send_message.call_args_list[0].args[1])

    def test_manual_save_reports_reuse_precommit_check_for_both_queries(self):
        with patch("beancounter.drafts.check_ledger", wraps=check_ledger) as checked, \
             patch("beancounter.ledger.check_ledger", wraps=check_ledger) as context_check:
            self.bot.handle_message({"message": {"chat": {"id": 123},
                "text": "Cafe\nLunch\nAssets:Cash -5 GBP\nExpenses:Food:Cafe 5 GBP"}})
        self.assertEqual(self.puts, 1)
        checked.assert_called_once()
        context_check.assert_not_called()
        self.assertEqual(self.bot.send_message.call_count, 3)

    def test_failed_save_never_reports(self):
        self.bot._github_put_file = lambda *args: (False, 500)
        self.bot.transaction_report_tables = MagicMock()
        self.bot.approve_pending("1", self.pending())
        self.bot.transaction_report_tables.assert_not_called()
        self.assertIn("1", self.bot.pending_llm_entries)

    def test_report_error_does_not_restore_or_resave_transaction(self):
        self.bot.run_bql = MagicMock(side_effect=ValueError("synthetic query failure"))
        self.bot.approve_pending("1", self.pending())
        self.assertEqual(self.puts, 1)
        self.assertNotIn("1", self.bot.pending_llm_entries)
        self.assertIn("无需重复记账", self.bot.send_message.call_args.args[1])

    def test_retry_after_review_rejection_reuses_identical_candidate_check(self):
        pending = self.pending()
        self.bot.review_journal.side_effect = [ValueError("synthetic rejection"), None]
        with patch("beancounter.drafts.check_ledger", wraps=check_ledger) as commit_check:
            with self.assertRaises(ValueError):
                self.bot.commit_llm_entry(pending)
            self.bot.commit_llm_entry(pending)
        commit_check.assert_called_once()
        self.assertEqual(self.bot.review_journal.call_count, 2)
        self.assertEqual(self.puts, 1)

    def test_retry_rechecks_when_ledger_changed_or_check_failed(self):
        pending = self.pending()
        self.bot.review_journal.side_effect = [ValueError("synthetic rejection"), None]
        with patch("beancounter.drafts.check_ledger", wraps=check_ledger) as commit_check:
            with self.assertRaises(ValueError):
                self.bot.commit_llm_entry(pending)
            self.texts["accounts.bean"] = ACCOUNTS + "2026-09-09 close Assets:Cash\n"
            self.tree = "account-closed"
            for _ in range(2):  # A failed check is never cached.
                with self.assertRaisesRegex(ValueError, "bean-check failed"):
                    self.bot.commit_llm_entry(pending)
        self.assertEqual(commit_check.call_count, 3)
        self.assertEqual(self.puts, 0)

    def publish_checked(self, appendix=ENTRY):
        return self.bot.publish_llm_draft(123, appendix, "entry", "Synthetic lunch 5 GBP", "2026-09-10", header="草稿：")

    def test_publication_checks_before_delivery_and_save_reuses_result(self):
        self.bot.send_draft_for_review = MagicMock()
        with patch("beancounter.drafts.check_ledger", wraps=check_ledger) as commit_check:
            def delivered(chat, header, appendix, pid):
                commit_check.assert_called_once()
                pending = self.bot.pending_llm_entries[pid]
                self.assertIn('datetime:', pending["commit_appendix"])
                self.assertIn('prompt:', pending["commit_appendix"])
                self.assertEqual(self.puts, 0)
            self.bot.send_draft_for_review.side_effect = delivered
            pid = self.publish_checked()
            self.bot.commit_llm_entry(self.bot.pending_llm_entries[pid])
        commit_check.assert_called_once()
        self.assertEqual(self.puts, 1)

    def test_identical_candidate_reuses_result_after_draft_lifetime(self):
        with patch("beancounter.drafts.check_ledger", wraps=check_ledger) as commit_check:
            pid = self.publish_checked()
            pending = self.bot.pending_llm_entries[pid]
            with patch("beancounter.drafts.time.monotonic", return_value=time.monotonic() + 86400):
                self.bot.commit_llm_entry(pending)
        commit_check.assert_called_once()
        self.assertEqual(self.puts, 1)

    def test_changed_ledger_after_publication_requires_another_check(self):
        with patch("beancounter.drafts.check_ledger", wraps=check_ledger) as commit_check:
            pid = self.publish_checked()
            self.texts["test.bean"] += '\n2026-09-08 * "Other writer"\n  Expenses:Food:Cafe 2 GBP\n  Assets:Cash -2 GBP\n'
            self.tree = "other-writer"
            self.bot.commit_llm_entry(self.bot.pending_llm_entries[pid])
        self.assertEqual(commit_check.call_count, 2)
        self.assertEqual(self.puts, 1)

    def test_invalid_candidate_cannot_be_published_or_cached(self):
        self.bot.send_draft_for_review = MagicMock()
        bad = '2026-09-10 * "Unbalanced"\n  Expenses:Food:Cafe 5 GBP\n  Assets:Cash -4 GBP'
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "bean-check failed"):
                self.publish_checked(bad)
        self.bot.send_draft_for_review.assert_not_called()
        self.assertEqual(self.bot.pending_llm_entries, {})
        self.assertIsNone(self.bot._commit_check_cache[0])
        self.assertEqual(self.puts, 0)

    def test_generation_publication_and_save_share_one_final_candidate_check(self):
        self.bot.llm_enabled = True
        self.bot._call_llm_backends = MagicMock(return_value=ENTRY)
        pending = self.bot._prepare_llm_pending(123, "entry", "Synthetic lunch 5 GBP", "2026-09-10")
        with patch("beancounter.drafts.check_ledger", wraps=check_ledger) as checked, \
             patch("beancounter.ledger.check_ledger", side_effect=AssertionError("no separate generation check")):
            entry = self.bot.call_openai_compatible("Synthetic lunch 5 GBP", self.bot.parse_accounts(),
                                                    "2026-09-10", pending=pending)
            checked.assert_called_once()
            pid = self.bot.publish_llm_draft(123, entry, "entry", "Synthetic lunch 5 GBP", "2026-09-10", header="草稿：", pending=pending)
            self.bot.commit_llm_entry(self.bot.pending_llm_entries[pid])
        checked.assert_called_once()
        self.assertEqual(self.puts, 1)

    def test_generation_repairs_full_candidate_before_publication(self):
        self.bot.llm_enabled = True
        bad = ENTRY.replace("Expenses:Food:Cafe", "Expenses:Travel").replace("GBP", "USD")
        self.bot._call_llm_backends = MagicMock(side_effect=[bad, ENTRY])
        pending = self.bot._prepare_llm_pending(123, "entry", "Synthetic lunch 5 GBP", "2026-09-10")
        with patch("beancounter.drafts.check_ledger", wraps=check_ledger) as checked:
            entry = self.bot.call_openai_compatible("Synthetic lunch 5 GBP", ["Assets:Cash", "Expenses:Food:Cafe", "Expenses:Travel"],
                                                    "2026-09-10", pending=pending)
            self.assertEqual(self.bot._call_llm_backends.call_count, 2)
            self.bot.publish_llm_draft(123, entry, "entry", "Synthetic lunch 5 GBP", "2026-09-10", header="草稿：", pending=pending)
        self.assertEqual(checked.call_count, 2)
        self.assertEqual(self.puts, 0)

    def test_review_can_be_disabled_without_skipping_bean_check(self):
        self.bot.settings.LLM_REVIEW_ENABLED = False
        self.bot.parse_accounts = MagicMock(side_effect=AssertionError("no review account refresh"))
        with patch("beancounter.drafts.check_ledger", wraps=check_ledger) as checked:
            self.bot.commit_llm_entry(self.pending())
        checked.assert_called_once()
        self.bot.review_journal.assert_not_called()
        self.assertEqual(self.puts, 1)

    def test_disabling_review_cannot_save_an_invalid_ledger(self):
        self.bot.settings.LLM_REVIEW_ENABLED = False
        self.texts["accounts.bean"] += "2026-09-09 close Assets:Cash\n"
        with self.assertRaisesRegex(ValueError, "bean-check failed"):
            self.bot.commit_llm_entry(self.pending())
        self.bot.review_journal.assert_not_called()
        self.assertEqual(self.puts, 0)

    def test_changed_remote_texts_must_be_rechecked(self):
        self.bot.commit_llm_entry(self.pending())
        self.texts["test.bean"] += '\n2026-09-11 * "Other writer"\n  Expenses:Food:Cafe 2 GBP\n  Assets:Cash -2 GBP\n'
        self.tree = "other-writer"
        with patch("beancounter.ledger.check_ledger", wraps=check_ledger) as checked:
            self.bot.transaction_report_tables(ENTRY)
        checked.assert_called_once()

    def test_nonledger_tree_change_reuses_identical_texts(self):
        first = self.bot.load_ledger()
        self.tree = "readme-change"
        with patch("beancounter.ledger.check_ledger", wraps=check_ledger) as checked:
            second = self.bot.load_ledger()
        checked.assert_not_called()
        self.assertIs(first[0], second[0])

    def test_failed_conflicting_candidate_is_not_used_for_report(self):
        original = self.bot._github_put_file
        def conflict(content, sha, message, file_path=None):
            self.texts["test.bean"] += '\n2026-09-09 * "Concurrent"\n  Expenses:Food:Cafe 100 GBP\n  Assets:Cash -100 GBP\n'
            self.tree = "conflict"
            self.bot._github_put_file = original
            return False, 409
        self.bot._github_put_file = conflict
        self.bot.commit_llm_entry(self.pending())
        with patch("beancounter.ledger.check_ledger", wraps=check_ledger) as checked:
            tables = self.bot.transaction_report_tables(ENTRY, today=date(2026, 9, 10))
        checked.assert_not_called()
        self.assertIn("123 GBP, 3 USD", tables[0][1])

    def test_bank_prefix_removed_and_collisions_remain_distinct(self):
        accounts = ["Assets:Bank:Demo:Current", "Assets:Cash", "Liabilities:Card"]
        self.assertEqual(_account_labels(accounts), dict(zip(accounts, ["Demo:Current", "Cash", "Card"])))
        ambiguous = ["Assets:Bank:Demo", "Liabilities:Demo", "Assets:Demo"]
        labels = _account_labels(ambiguous)
        self.assertEqual(len(set(labels.values())), 3)
        self.assertEqual(labels["Assets:Bank:Demo"], "Bank:Demo")
        self.assertEqual(labels["Assets:Demo"], "Assets:Demo")

    def test_telegram_uses_escaped_code_blocks_with_length_limit(self):
        self.bot.transaction_report_tables = MagicMock(return_value=[
            ("本月开支", "Food  5 GBP\nCafe <Demo> & 😀"),
            ("账户余额", "Cash " + "😀" * 3000),
        ])
        self.bot.send_transaction_report(123, ENTRY)
        self.assertEqual(self.bot.send_message.call_count, 2)
        for call in self.bot.send_message.call_args_list:
            text = call.args[1]
            self.assertEqual(call.kwargs["parse_mode"], "HTML")
            self.assertIn("<pre>", text)
            self.assertIn("</pre>", text)
            self.assertLessEqual(_utf16_len(text), 4096)
        self.assertIn("&lt;Demo&gt; &amp;", self.bot.send_message.call_args_list[0].args[1])
        self.assertIn("已截断", self.bot.send_message.call_args_list[1].args[1])

    def test_report_send_failure_does_not_escape(self):
        self.bot.transaction_report_tables = MagicMock(return_value=[("开支", "Food 5 GBP")])
        self.bot.send_message.side_effect = RuntimeError("synthetic Telegram failure")
        self.bot.send_transaction_report(123, ENTRY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
