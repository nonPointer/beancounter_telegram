"""Real BQL and post-save snapshot reuse with synthetic journals and no network."""

import hashlib
import os
import sys
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

    def test_manual_save_reports_load_once_for_both_queries(self):
        with patch("beancounter.ledger.check_ledger", wraps=check_ledger) as checked:
            self.bot.handle_message({"message": {"chat": {"id": 123},
                "text": "Cafe\nLunch\nAssets:Cash -5 GBP\nExpenses:Food:Cafe 5 GBP"}})
        self.assertEqual(self.puts, 1)
        checked.assert_called_once()
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
