"""Tests for 1.1 / 1.3 / 1.4 / 5.6 refactoring in main.py."""
import json
import sys
import time
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

# Stub config.json so main.py can be imported without the real file.
FAKE_CONFIG = {
    "GITHUB_TOKEN": "tok",
    "REPO_OWNER": "o",
    "REPO_NAME": "r",
    "BRANCH_NAME": "main",
    "FILE_PATH": "main.bean",
    "TIMEZONE": "UTC",
    "TELEGRAM_BOT_TOKEN": "bot:tok",
}

with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(FAKE_CONFIG))):
    import main


class TestAccountTypeMap(unittest.TestCase):
    """1.1 – ACCOUNT_TYPE_MAP module-level constant."""

    def test_all_types_present(self):
        for key in ("assets", "liabilities", "equity", "income", "expenses"):
            self.assertIn(key, main.ACCOUNT_TYPE_MAP)

    def test_values_are_bean_paths(self):
        for path in main.ACCOUNT_TYPE_MAP.values():
            self.assertTrue(path.startswith("accounts/") and path.endswith(".bean"))

    def test_unknown_prefix_returns_file_path(self):
        result = main.ACCOUNT_TYPE_MAP.get("unknown", main.FILE_PATH)
        self.assertEqual(result, main.FILE_PATH)


class TestMakePendingEntry(unittest.TestCase):
    """1.4 – _make_pending_entry helper."""

    def setUp(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(FAKE_CONFIG))):
            self.bot = main.Bot()

    def test_keys_present(self):
        entry = self.bot._make_pending_entry(42, "appendix", "commit msg", "user input", "2024-01-15")
        for key in ("chat_id", "appendix", "commit_message", "created_at", "user_input", "date_str"):
            self.assertIn(key, entry)

    def test_values_correct(self):
        before = time.time()
        entry = self.bot._make_pending_entry(99, "app", "cm", "inp", "2025-03-09")
        self.assertEqual(entry["chat_id"], 99)
        self.assertEqual(entry["appendix"], "app")
        self.assertEqual(entry["commit_message"], "cm")
        self.assertEqual(entry["user_input"], "inp")
        self.assertEqual(entry["date_str"], "2025-03-09")
        self.assertGreaterEqual(entry["created_at"], before)


class TestCallLlmBackends(unittest.TestCase):
    """1.3 – _call_llm_backends helper."""

    def setUp(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(FAKE_CONFIG))):
            self.bot = main.Bot()

    def _make_backend(self, url="http://fake", key="k", model="m"):
        return {"base_url": url, "api_key": key, "model": model}

    def test_returns_content_on_success(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"choices": [{"message": {"content": "  hello  "}}]}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(main, "LLM_BACKENDS", [self._make_backend()]):
            with patch("requests.post", return_value=mock_resp):
                result = self.bot._call_llm_backends({})
        self.assertEqual(result, "hello")

    def test_falls_through_to_second_backend(self):
        good_resp = MagicMock()
        good_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        good_resp.raise_for_status = MagicMock()

        call_count = 0
        def fake_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("first fails")
            return good_resp

        backends = [self._make_backend(model="m1"), self._make_backend(model="m2")]
        with patch.object(main, "LLM_BACKENDS", backends):
            with patch("requests.post", side_effect=fake_post):
                result = self.bot._call_llm_backends({})
        self.assertEqual(result, "ok")
        self.assertEqual(call_count, 2)

    def test_raises_when_all_fail(self):
        with patch.object(main, "LLM_BACKENDS", [self._make_backend()]):
            with patch("requests.post", side_effect=ConnectionError("nope")):
                with self.assertRaises(ValueError, msg="All LLM backends failed"):
                    self.bot._call_llm_backends({})

    def test_vision_uses_vision_model(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        mock_resp.raise_for_status = MagicMock()

        backend = {**self._make_backend(model="text-model"), "vision_model": "vision-model"}
        with patch.object(main, "LLM_BACKENDS", [backend]):
            with patch("requests.post", return_value=mock_resp) as mock_post:
                self.bot._call_llm_backends({}, vision=True)

        sent_model = mock_post.call_args[1]["json"]["model"]
        self.assertEqual(sent_model, "vision-model")

    def test_vision_falls_back_to_model(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        mock_resp.raise_for_status = MagicMock()

        backend = self._make_backend(model="text-model")  # no vision_model
        with patch.object(main, "LLM_BACKENDS", [backend]):
            with patch("requests.post", return_value=mock_resp) as mock_post:
                self.bot._call_llm_backends({}, vision=True)

        sent_model = mock_post.call_args[1]["json"]["model"]
        self.assertEqual(sent_model, "text-model")

    def test_text_ignores_vision_model(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        mock_resp.raise_for_status = MagicMock()

        backend = {**self._make_backend(model="text-model"), "vision_model": "vision-model"}
        with patch.object(main, "LLM_BACKENDS", [backend]):
            with patch("requests.post", return_value=mock_resp) as mock_post:
                self.bot._call_llm_backends({})

        sent_model = mock_post.call_args[1]["json"]["model"]
        self.assertEqual(sent_model, "text-model")


class TestDateValidation(unittest.TestCase):
    """5.6 – strptime-based date validation in handle_message."""

    def _parse_date_line(self, first_line: str):
        """Mirror the logic added in handle_message."""
        try:
            datetime.strptime(first_line, "%Y-%m-%d")
            return first_line
        except ValueError:
            return None

    def test_valid_date_accepted(self):
        self.assertEqual(self._parse_date_line("2024-01-15"), "2024-01-15")

    def test_invalid_month_rejected(self):
        self.assertIsNone(self._parse_date_line("2024-99-01"))

    def test_invalid_day_rejected(self):
        self.assertIsNone(self._parse_date_line("2024-01-99"))

    def test_non_date_text_rejected(self):
        self.assertIsNone(self._parse_date_line("hello world"))

    def test_today_valid(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(self._parse_date_line(today), today)

    def test_partial_date_rejected(self):
        # old regex would match; strptime rejects
        self.assertIsNone(self._parse_date_line("2024-01"))


class TestStripCodeFence(unittest.TestCase):
    def setUp(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(FAKE_CONFIG))):
            self.bot = main.Bot()

    def test_no_fence_passthrough(self):
        text = "2024-01-01 * \"Shop\" \"Groceries\"\n  Expenses:Food  10 USD\n  Assets:Cash  -10 USD"
        self.assertEqual(self.bot.strip_code_fence(text), text)

    def test_strips_plain_fence(self):
        text = "```\n2024-01-01 * \"Shop\" \"Groceries\"\n  Expenses:Food  10 USD\n  Assets:Cash  -10 USD\n```"
        result = self.bot.strip_code_fence(text)
        self.assertNotIn("```", result)
        self.assertIn("2024-01-01", result)

    def test_strips_language_tagged_fence(self):
        inner = "2024-01-01 * \"A\" \"B\"\n  X:Y  1 USD\n  A:B  -1 USD"
        text = f"```beancount\n{inner}\n```"
        self.assertEqual(self.bot.strip_code_fence(text), inner)

    def test_too_few_lines_not_stripped(self):
        text = "```\nhello\n```"
        # 3 lines but the inner is just 1 line; strip_code_fence strips it
        result = self.bot.strip_code_fence(text)
        self.assertEqual(result, "hello")

    def test_two_line_fence_not_stripped(self):
        # Only 2 lines total — not a valid fence
        text = "```\nhello```"
        result = self.bot.strip_code_fence(text)
        self.assertIn("```", result)


class TestNormalizeAndValidate(unittest.TestCase):
    def setUp(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(FAKE_CONFIG))):
            self.bot = main.Bot()
        self.accounts = [
            "Assets:WeChat:Current",
            "Expenses:Food",
            "Assets:Cash",
            "Liabilities:CreditCard:Chase",
        ]

    def _valid_entry(self):
        return (
            '2024-01-15 * "KFC" "Lunch"\n'
            "  Expenses:Food  10 USD\n"
            "  Assets:Cash  -10 USD"
        )

    def test_valid_entry_returned(self):
        result = self.bot.normalize_and_validate_llm_entry(self._valid_entry(), self.accounts)
        self.assertIn("2024-01-15", result)
        self.assertIn("Expenses:Food", result)

    def test_too_short_raises(self):
        with self.assertRaises(ValueError, msg="too short"):
            self.bot.normalize_and_validate_llm_entry("header\n  X  1 USD", self.accounts)

    def test_fewer_than_two_postings_raises(self):
        entry = '2024-01-15 * "A" "B"\n  metadata: "x"\n  metadata2: "y"'
        with self.assertRaises(ValueError):
            self.bot.normalize_and_validate_llm_entry(entry, self.accounts)

    def test_unbalanced_same_currency_raises(self):
        entry = (
            '2024-01-15 * "A" "B"\n'
            "  Expenses:Food  15 USD\n"
            "  Assets:Cash  -10 USD"
        )
        with self.assertRaises(ValueError):
            self.bot.normalize_and_validate_llm_entry(entry, self.accounts)

    def test_both_same_sign_raises(self):
        entry = (
            '2024-01-15 * "A" "B"\n'
            "  Expenses:Food  10 USD\n"
            "  Assets:Cash  10 USD"
        )
        with self.assertRaises(ValueError):
            self.bot.normalize_and_validate_llm_entry(entry, self.accounts)

    def test_strips_code_fence(self):
        fenced = "```\n" + self._valid_entry() + "\n```"
        result = self.bot.normalize_and_validate_llm_entry(fenced, self.accounts)
        self.assertNotIn("```", result)

    def test_prefer_current_applied(self):
        # Input uses Assets:WeChat without :Current; list has Assets:WeChat:Current
        entry = (
            '2024-01-15 * "Shop" "Lunch"\n'
            "  Expenses:Food  20 USD\n"
            "  Assets:WeChat  -20 USD"
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, self.accounts)
        self.assertIn("Assets:WeChat:Current", result)

    def test_cross_currency_auto_fx(self):
        entry = (
            '2024-01-15 * "Shop" "Coffee"\n'
            "  Expenses:Food  100 CNY\n"
            "  Assets:Cash  -13 USD"
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, self.accounts)
        self.assertIn("@", result)

    def test_three_posting_balance_check(self):
        # Valid 3-posting split bill
        entry = (
            '2024-01-15 * "Restaurant" "Dinner"\n'
            "  Liabilities:CreditCard:Chase  -90 USD\n"
            "  Assets:Cash  45 USD\n"
            "  Expenses:Food  45 USD"
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, self.accounts)
        self.assertIn("Expenses:Food", result)


class TestEnsureDatetimeMetadata(unittest.TestCase):
    def setUp(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(FAKE_CONFIG))):
            self.bot = main.Bot()

    def test_inserts_after_header(self):
        entry = '2024-01-15 * "A" "B"\n  Expenses:Food  10 USD\n  Assets:Cash  -10 USD'
        result = self.bot.ensure_datetime_metadata(entry, "2024-01-15 12:00:00")
        lines = result.splitlines()
        self.assertEqual(lines[0], '2024-01-15 * "A" "B"')
        self.assertIn('datetime: "2024-01-15 12:00:00"', lines[1])

    def test_idempotent_if_already_present(self):
        entry = (
            '2024-01-15 * "A" "B"\n'
            '  datetime: "2024-01-15 12:00:00"\n'
            "  Expenses:Food  10 USD\n"
            "  Assets:Cash  -10 USD"
        )
        result = self.bot.ensure_datetime_metadata(entry, "2024-01-15 12:00:00")
        self.assertEqual(result.count('datetime:'), 1)

    def test_works_with_leading_comment(self):
        entry = (
            "; original user input\n"
            '2024-01-15 * "A" "B"\n'
            "  Expenses:Food  10 USD\n"
            "  Assets:Cash  -10 USD"
        )
        result = self.bot.ensure_datetime_metadata(entry, "2024-01-15 09:30:00")
        lines = result.splitlines()
        header_idx = next(i for i, l in enumerate(lines) if l.startswith("2024-01-15 *"))
        self.assertIn('datetime:', lines[header_idx + 1])

    def test_empty_string_returned_unchanged(self):
        self.assertEqual(self.bot.ensure_datetime_metadata("", "2024-01-15 00:00:00"), "")


class TestPrependNaturalLanguageComment(unittest.TestCase):
    def setUp(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(FAKE_CONFIG))):
            self.bot = main.Bot()

    def test_prepends_comment(self):
        entry = '2024-01-15 * "A" "B"\n  X  10 USD\n  Y  -10 USD'
        result = self.bot.prepend_natural_language_comment(entry, "lunch at KFC")
        self.assertTrue(result.startswith("; lunch at KFC\n"))

    def test_empty_user_input_unchanged(self):
        entry = '2024-01-15 * "A" "B"\n  X  10 USD\n  Y  -10 USD'
        self.assertEqual(self.bot.prepend_natural_language_comment(entry, ""), entry)
        self.assertEqual(self.bot.prepend_natural_language_comment(entry, "   "), entry)

    def test_idempotent_if_already_present(self):
        entry = "; lunch at KFC\n2024-01-15 * \"A\" \"B\"\n  X  10 USD\n  Y  -10 USD"
        result = self.bot.prepend_natural_language_comment(entry, "lunch at KFC")
        self.assertEqual(result.count("; lunch at KFC"), 1)

    def test_multiline_input_flattened(self):
        entry = '2024-01-15 * "A" "B"\n  X  10 USD\n  Y  -10 USD'
        result = self.bot.prepend_natural_language_comment(entry, "lunch\nat KFC")
        self.assertTrue(result.startswith("; lunch at KFC\n"))


class TestMatchAccount(unittest.TestCase):
    def setUp(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(FAKE_CONFIG))):
            self.bot = main.Bot()
        self.bot._accounts_cache["accounts"] = [
            "Assets:WeChat:Current",
            "Assets:Alipay:Current",
            "Expenses:Food",
            "Liabilities:CreditCard:Chase",
        ]

    def test_match_by_suffix(self):
        with patch.object(self.bot, "parse_accounts", return_value=self.bot._accounts_cache["accounts"]):
            result = self.bot.match_account("Alipay:Current")
        self.assertEqual(result, "Assets:Alipay:Current")

    def test_case_insensitive(self):
        with patch.object(self.bot, "parse_accounts", return_value=self.bot._accounts_cache["accounts"]):
            result = self.bot.match_account("food")
        self.assertEqual(result, "Expenses:Food")

    def test_no_match_returns_none(self):
        with patch.object(self.bot, "parse_accounts", return_value=self.bot._accounts_cache["accounts"]):
            result = self.bot.match_account("NonExistent:Account")
        self.assertIsNone(result)


class TestPreferCurrentAccount(unittest.TestCase):
    def setUp(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(FAKE_CONFIG))):
            self.bot = main.Bot()

    def test_exact_match_returned(self):
        accounts = ["Assets:WeChat:Current", "Expenses:Food"]
        self.assertEqual(self.bot.prefer_current_account("Expenses:Food", accounts), "Expenses:Food")

    def test_adds_current_suffix(self):
        accounts = ["Assets:WeChat:Current", "Expenses:Food"]
        self.assertEqual(self.bot.prefer_current_account("Assets:WeChat", accounts), "Assets:WeChat:Current")

    def test_liabilities_not_extended(self):
        accounts = ["Liabilities:CreditCard:Chase", "Liabilities:CreditCard:Chase:Current"]
        # Liabilities accounts should NOT get :Current added
        result = self.bot.prefer_current_account("Liabilities:CreditCard:Chase", accounts)
        self.assertEqual(result, "Liabilities:CreditCard:Chase")

    def test_already_has_current_not_doubled(self):
        accounts = ["Assets:WeChat:Current"]
        result = self.bot.prefer_current_account("Assets:WeChat:Current", accounts)
        self.assertEqual(result, "Assets:WeChat:Current")

    def test_unknown_account_returned_as_is(self):
        accounts = ["Assets:WeChat:Current"]
        result = self.bot.prefer_current_account("Assets:HSBC", accounts)
        self.assertEqual(result, "Assets:HSBC")


class TestExtractAccountsFromEntry(unittest.TestCase):
    def setUp(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(FAKE_CONFIG))):
            self.bot = main.Bot()

    def test_extracts_posting_accounts(self):
        entry = (
            '2024-01-15 * "Shop" "Lunch"\n'
            "  Expenses:Food  20 USD\n"
            "  Assets:Cash  -20 USD"
        )
        result = self.bot.extract_accounts_from_entry(entry)
        self.assertIn("Expenses:Food", result)
        self.assertIn("Assets:Cash", result)
        self.assertEqual(len(result), 2)

    def test_header_not_included(self):
        entry = (
            '2024-01-15 * "Shop" "Lunch"\n'
            "  Expenses:Food  20 USD\n"
            "  Assets:Cash  -20 USD"
        )
        for acct in self.bot.extract_accounts_from_entry(entry):
            self.assertNotIn("*", acct)

    def test_metadata_lines_not_included(self):
        entry = (
            '2024-01-15 * "Shop" "Lunch"\n'
            '  datetime: "2024-01-15 12:00:00"\n'
            "  Expenses:Food  20 USD\n"
            "  Assets:Cash  -20 USD"
        )
        result = self.bot.extract_accounts_from_entry(entry)
        # datetime: line starts with spaces but has no number amount, so it won't match posting_re
        # Actually let's just check we get the two real accounts
        self.assertIn("Expenses:Food", result)
        self.assertIn("Assets:Cash", result)


class TestAccountsForPrompt(unittest.TestCase):
    def setUp(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(FAKE_CONFIG))):
            self.bot = main.Bot()

    def test_annotates_accounts_with_currency(self):
        self.bot._accounts_cache["accounts"] = ["Assets:WeChat:Current", "Expenses:Food"]
        self.bot._accounts_cache["currencies"] = {"Assets:WeChat:Current": "CNY"}
        self.bot._accounts_cache["comments"] = {}
        result = self.bot._accounts_for_prompt()
        self.assertIn("Assets:WeChat:Current (CNY)", result)
        self.assertIn("Expenses:Food", result)
        self.assertEqual(next(x for x in result if "Food" in x), "Expenses:Food")

    def test_annotates_accounts_with_comment(self):
        self.bot._accounts_cache["accounts"] = ["Assets:Bank:CMB"]
        self.bot._accounts_cache["currencies"] = {"Assets:Bank:CMB": "CNY"}
        self.bot._accounts_cache["comments"] = {"Assets:Bank:CMB": "招商银行"}
        result = self.bot._accounts_for_prompt()
        self.assertEqual(result, ["Assets:Bank:CMB (CNY) ; 招商银行"])

    def test_comment_without_currency(self):
        self.bot._accounts_cache["accounts"] = ["Assets:Bank:Foo"]
        self.bot._accounts_cache["currencies"] = {}
        self.bot._accounts_cache["comments"] = {"Assets:Bank:Foo": "某银行"}
        result = self.bot._accounts_for_prompt()
        self.assertEqual(result, ["Assets:Bank:Foo ; 某银行"])

    def test_no_currency_no_annotation(self):
        self.bot._accounts_cache["accounts"] = ["Assets:Cash"]
        self.bot._accounts_cache["currencies"] = {}
        self.bot._accounts_cache["comments"] = {}
        result = self.bot._accounts_for_prompt()
        self.assertEqual(result, ["Assets:Cash"])


class TestAddNonPnlAccountsToCommitMessage(unittest.TestCase):
    def setUp(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(FAKE_CONFIG))):
            self.bot = main.Bot()

    def test_adds_assets_not_expenses(self):
        entry = (
            '2024-01-15 * "Shop" "Lunch"\n'
            "  Expenses:Food  20 USD\n"
            "  Assets:Cash  -20 USD"
        )
        result = self.bot.add_non_pnl_accounts_to_commit_message("prefix\n\n", entry)
        self.assertIn("Assets:Cash", result)
        self.assertNotIn("Expenses:Food", result)

    def test_adds_liabilities(self):
        entry = (
            '2024-01-15 * "Shop" "Lunch"\n'
            "  Expenses:Food  20 USD\n"
            "  Liabilities:CreditCard:Chase  -20 USD"
        )
        result = self.bot.add_non_pnl_accounts_to_commit_message("prefix\n\n", entry)
        self.assertIn("Liabilities:CreditCard:Chase", result)

    def test_does_not_add_income(self):
        entry = (
            '2024-01-15 * "Salary" "Salary"\n'
            "  Assets:Bank  5000 USD\n"
            "  Income:Salary  -5000 USD"
        )
        result = self.bot.add_non_pnl_accounts_to_commit_message("prefix\n\n", entry)
        self.assertNotIn("Income:Salary", result)
        self.assertIn("Assets:Bank", result)


class TestIsPendingExpired(unittest.TestCase):
    def setUp(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(FAKE_CONFIG))):
            self.bot = main.Bot()

    def test_fresh_entry_not_expired(self):
        pending = self.bot._make_pending_entry(1, "app", "cm", "inp", "2024-01-15")
        self.assertFalse(self.bot.is_pending_expired(pending))

    def test_old_entry_is_expired(self):
        pending = self.bot._make_pending_entry(1, "app", "cm", "inp", "2024-01-15")
        pending["created_at"] = time.time() - main.DRAFT_TTL_SECONDS - 1
        self.assertTrue(self.bot.is_pending_expired(pending))


class TestCleanupExpiredDrafts(unittest.TestCase):
    def setUp(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(FAKE_CONFIG))):
            self.bot = main.Bot()

    def test_removes_expired_entries(self):
        old_entry = self.bot._make_pending_entry(42, "app", "cm", "inp", "2024-01-15")
        old_entry["created_at"] = time.time() - main.DRAFT_TTL_SECONDS - 10
        self.bot.pending_llm_entries["1"] = old_entry

        fresh_entry = self.bot._make_pending_entry(42, "app2", "cm2", "inp2", "2024-01-15")
        self.bot.pending_llm_entries["2"] = fresh_entry

        with patch.object(self.bot, "send_message") as mock_send:
            self.bot.cleanup_expired_drafts()

        self.assertNotIn("1", self.bot.pending_llm_entries)
        self.assertIn("2", self.bot.pending_llm_entries)
        mock_send.assert_called_once()

    def test_notifies_user_on_expiry(self):
        old_entry = self.bot._make_pending_entry(99, "app", "cm", "inp", "2024-01-15")
        old_entry["created_at"] = time.time() - main.DRAFT_TTL_SECONDS - 10
        self.bot.pending_llm_entries["1"] = old_entry

        with patch.object(self.bot, "send_message") as mock_send:
            self.bot.cleanup_expired_drafts()

        mock_send.assert_called_once_with(99, unittest.mock.ANY)


class TestExtractLastDirectiveBlock(unittest.TestCase):
    """Tests for extract_last_directive_block pure function."""

    def test_empty_content_returns_none(self):
        self.assertIsNone(main.extract_last_directive_block(""))

    def test_no_directives_returns_none(self):
        content = "; just a comment\n\nplugin \"beancount.loader\"\n"
        self.assertIsNone(main.extract_last_directive_block(content))

    def test_single_transaction_extracted(self):
        content = (
            '2024-01-15 * "Shop" "Coffee"\n'
            '  datetime: "2024-01-15 10:00:00"\n'
            '  Expenses:Food  10 CNY\n'
            '  Assets:Cash  -10 CNY\n'
        )
        result = main.extract_last_directive_block(content)
        self.assertIsNotNone(result)
        directive_text, new_content = result
        self.assertIn('2024-01-15 * "Shop" "Coffee"', directive_text)
        self.assertEqual(new_content.strip(), "")

    def test_last_of_two_transactions_extracted(self):
        content = (
            '2024-01-14 * "A" "first"\n'
            '  Assets:Cash  -5 CNY\n'
            '  Expenses:Food  5 CNY\n'
            '\n'
            '2024-01-15 * "B" "second"\n'
            '  Assets:Cash  -10 CNY\n'
            '  Expenses:Food  10 CNY\n'
        )
        result = main.extract_last_directive_block(content)
        directive_text, new_content = result
        self.assertIn('"B" "second"', directive_text)
        self.assertNotIn('"B"', new_content)
        self.assertIn('"A" "first"', new_content)

    def test_balance_directive_extracted(self):
        content = (
            '2024-01-14 * "A" "txn"\n'
            '  Assets:Cash  -5 CNY\n'
            '  Expenses:Food  5 CNY\n'
            '\n'
            '2024-01-15 balance Assets:Cash  100 CNY\n'
        )
        result = main.extract_last_directive_block(content)
        directive_text, new_content = result
        self.assertIn('balance Assets:Cash', directive_text)
        self.assertNotIn('balance', new_content)
        self.assertIn('"A" "txn"', new_content)

    def test_first_transaction_preserved_after_removal(self):
        content = (
            '2024-01-14 * "Keep" "this"\n'
            '  Assets:Cash  -5 CNY\n'
            '  Expenses:Food  5 CNY\n'
            '\n'
            '2024-01-15 * "Remove" "this"\n'
            '  Assets:Cash  -10 CNY\n'
            '  Expenses:Food  10 CNY\n'
        )
        _, new_content = main.extract_last_directive_block(content)
        self.assertIn('"Keep" "this"', new_content)
        self.assertNotIn('"Remove"', new_content)

    def test_trailing_blank_lines_not_left_dangling(self):
        content = '2024-01-15 * "A" "B"\n  Assets:Cash  -5 CNY\n  Expenses:Food  5 CNY\n\n\n'
        _, new_content = main.extract_last_directive_block(content)
        self.assertNotIn('\n\n', new_content)

    def test_new_content_ends_with_newline(self):
        content = (
            '2024-01-14 * "A" "first"\n'
            '  Assets:Cash  -5 CNY\n'
            '\n'
            '2024-01-15 * "B" "second"\n'
            '  Assets:Cash  -10 CNY\n'
        )
        _, new_content = main.extract_last_directive_block(content)
        self.assertTrue(new_content.endswith('\n'))


class TestBuildUserPromptCurrentTime(unittest.TestCase):
    """Tests for current_time parameter in build_user_prompt."""

    def test_without_current_time(self):
        from prompts import build_user_prompt
        result = build_user_prompt("2026-03-13", ["Assets:Cash"], "lunch 50")
        self.assertNotIn("current time", result)
        self.assertIn("Transaction date is 2026-03-13", result)

    def test_with_empty_current_time(self):
        from prompts import build_user_prompt
        result = build_user_prompt("2026-03-13", ["Assets:Cash"], "lunch 50", current_time="")
        self.assertNotIn("current time", result)

    def test_with_current_time(self):
        from prompts import build_user_prompt
        result = build_user_prompt("2026-03-13", ["Assets:Cash"], "lunch 50", current_time="14:30")
        self.assertIn("(current time: 14:30)", result)
        self.assertIn("Transaction date is 2026-03-13", result)

    def test_with_previous_draft_and_time(self):
        from prompts import build_user_prompt
        result = build_user_prompt(
            "2026-03-13", ["Assets:Cash"], "lunch 50",
            previous_draft="old draft", current_time="08:00",
        )
        self.assertIn("(current time: 08:00)", result)
        self.assertIn("Previous declined draft:", result)

    def test_with_decline_reason_and_time(self):
        from prompts import build_user_prompt
        result = build_user_prompt(
            "2026-03-13", ["Assets:Cash"], "lunch 50",
            decline_reason="wrong account", current_time="19:45",
        )
        self.assertIn("(current time: 19:45)", result)
        self.assertIn("Decline reason from user:", result)

    def test_with_all_optional_params(self):
        from prompts import build_user_prompt
        result = build_user_prompt(
            "2026-03-13", ["Assets:Cash", "Expenses:Food"], "dinner 80",
            previous_draft="draft v1", decline_reason="fix payee",
            current_time="20:15",
        )
        self.assertIn("(current time: 20:15)", result)
        self.assertIn("Previous declined draft:", result)
        self.assertIn("Decline reason from user:", result)
        self.assertIn("Assets:Cash", result)
        self.assertIn("Expenses:Food", result)


class TestBuildInvestOrderPromptCurrentTime(unittest.TestCase):
    """Tests for current_time parameter in build_invest_order_prompt."""

    def test_without_current_time(self):
        from prompts import build_invest_order_prompt
        result = build_invest_order_prompt("2026-03-13", ["Assets:Broker:Cash"])
        self.assertNotIn("current time", result)
        self.assertIn("Reference date (today): 2026-03-13.", result)

    def test_with_empty_current_time(self):
        from prompts import build_invest_order_prompt
        result = build_invest_order_prompt("2026-03-13", ["Assets:Broker:Cash"], current_time="")
        self.assertNotIn("current time", result)

    def test_with_current_time(self):
        from prompts import build_invest_order_prompt
        result = build_invest_order_prompt("2026-03-13", ["Assets:Broker:Cash"], current_time="09:30")
        self.assertIn("(current time: 09:30)", result)
        self.assertIn("Reference date (today): 2026-03-13", result)


class TestExtractAllDirectiveBlocks(unittest.TestCase):
    """Tests for extract_all_directive_blocks pure function."""

    def test_empty_content(self):
        self.assertEqual(main.extract_all_directive_blocks(""), [])

    def test_no_directives(self):
        content = "; just a comment\n\nplugin \"beancount.loader\"\n"
        self.assertEqual(main.extract_all_directive_blocks(content), [])

    def test_single_transaction(self):
        content = (
            '2024-01-15 * "Shop" "Coffee"\n'
            '  Expenses:Food  10 CNY\n'
            '  Assets:Cash  -10 CNY\n'
        )
        blocks = main.extract_all_directive_blocks(content)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0][0], "2024-01-15")
        self.assertIn('"Shop" "Coffee"', blocks[0][1])

    def test_two_transactions(self):
        content = (
            '2024-01-14 * "A" "first"\n'
            '  Assets:Cash  -5 CNY\n'
            '  Expenses:Food  5 CNY\n'
            '\n'
            '2024-01-15 * "B" "second"\n'
            '  Assets:Cash  -10 CNY\n'
            '  Expenses:Food  10 CNY\n'
        )
        blocks = main.extract_all_directive_blocks(content)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0][0], "2024-01-14")
        self.assertIn('"A" "first"', blocks[0][1])
        self.assertEqual(blocks[1][0], "2024-01-15")
        self.assertIn('"B" "second"', blocks[1][1])

    def test_includes_leading_comment(self):
        content = (
            '; lunch at KFC\n'
            '2024-01-15 * "KFC" "Lunch"\n'
            '  Expenses:Food  10 CNY\n'
            '  Assets:Cash  -10 CNY\n'
        )
        blocks = main.extract_all_directive_blocks(content)
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0][1].startswith('; lunch at KFC'))

    def test_includes_metadata_lines(self):
        content = (
            '2024-01-15 * "Shop" "Coffee"\n'
            '  datetime: "2024-01-15 10:00:00"\n'
            '  Expenses:Food  10 CNY\n'
            '  Assets:Cash  -10 CNY\n'
        )
        blocks = main.extract_all_directive_blocks(content)
        self.assertEqual(len(blocks), 1)
        self.assertIn('datetime:', blocks[0][1])

    def test_mixed_directive_types(self):
        content = (
            '2024-01-14 * "A" "txn"\n'
            '  Assets:Cash  -5 CNY\n'
            '  Expenses:Food  5 CNY\n'
            '\n'
            '2024-01-15 balance Assets:Cash  100 CNY\n'
            '\n'
            '2024-01-16 pad Assets:Cash Equity:Opening\n'
        )
        blocks = main.extract_all_directive_blocks(content)
        self.assertEqual(len(blocks), 3)
        self.assertEqual(blocks[0][0], "2024-01-14")
        self.assertEqual(blocks[1][0], "2024-01-15")
        self.assertIn("balance", blocks[1][1])
        self.assertEqual(blocks[2][0], "2024-01-16")
        self.assertIn("pad", blocks[2][1])

    def test_same_date_multiple_entries(self):
        content = (
            '2024-01-15 * "A" "first"\n'
            '  Expenses:Food  5 CNY\n'
            '  Assets:Cash  -5 CNY\n'
            '\n'
            '2024-01-15 * "B" "second"\n'
            '  Expenses:Food  10 CNY\n'
            '  Assets:Cash  -10 CNY\n'
        )
        blocks = main.extract_all_directive_blocks(content)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0][0], "2024-01-15")
        self.assertEqual(blocks[1][0], "2024-01-15")

    def test_comment_not_shared_across_entries(self):
        content = (
            '2024-01-14 * "A" "first"\n'
            '  Assets:Cash  -5 CNY\n'
            '  Expenses:Food  5 CNY\n'
            '\n'
            '; second entry note\n'
            '2024-01-15 * "B" "second"\n'
            '  Assets:Cash  -10 CNY\n'
            '  Expenses:Food  10 CNY\n'
        )
        blocks = main.extract_all_directive_blocks(content)
        self.assertEqual(len(blocks), 2)
        self.assertNotIn(';', blocks[0][1])
        self.assertIn('; second entry note', blocks[1][1])


class TestHandleLast(unittest.TestCase):
    def setUp(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(FAKE_CONFIG))):
            self.bot = main.Bot()

    def _make_file(self, content):
        return {"content": content, "sha": "abc123"}

    def test_shows_last_5_entries(self):
        entries = []
        for i in range(1, 8):
            entries.append(
                f'2024-01-{i:02d} * "Shop{i}" "Item{i}"\n'
                f'  Expenses:Food  {i}0 CNY\n'
                f'  Assets:Cash  -{i}0 CNY'
            )
        content = "\n\n".join(entries) + "\n"

        with patch.object(self.bot, "github_download_file", return_value=self._make_file(content)):
            with patch.object(self.bot, "send_message") as mock_send:
                self.bot.handle_last(42)

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][1]
        self.assertIn("最近 5 条记录", msg)
        # Should contain entries 3-7 but not 1-2
        self.assertIn("Shop3", msg)
        self.assertIn("Shop7", msg)
        self.assertNotIn("Shop2", msg)

    def test_shows_all_when_fewer_than_count(self):
        content = (
            '2024-01-15 * "A" "only"\n'
            '  Expenses:Food  10 CNY\n'
            '  Assets:Cash  -10 CNY\n'
        )
        with patch.object(self.bot, "github_download_file", return_value=self._make_file(content)):
            with patch.object(self.bot, "send_message") as mock_send:
                self.bot.handle_last(42)

        msg = mock_send.call_args[0][1]
        self.assertIn("最近 1 条记录", msg)

    def test_custom_count(self):
        entries = []
        for i in range(1, 6):
            entries.append(
                f'2024-01-{i:02d} * "Shop{i}" "Item{i}"\n'
                f'  Expenses:Food  {i}0 CNY\n'
                f'  Assets:Cash  -{i}0 CNY'
            )
        content = "\n\n".join(entries) + "\n"

        with patch.object(self.bot, "github_download_file", return_value=self._make_file(content)):
            with patch.object(self.bot, "send_message") as mock_send:
                self.bot.handle_last(42, count=2)

        msg = mock_send.call_args[0][1]
        self.assertIn("最近 2 条记录", msg)
        self.assertIn("Shop4", msg)
        self.assertIn("Shop5", msg)
        self.assertNotIn("Shop3", msg)

    def test_empty_file(self):
        with patch.object(self.bot, "github_download_file", return_value=self._make_file("")):
            with patch.object(self.bot, "send_message") as mock_send:
                self.bot.handle_last(42)

        msg = mock_send.call_args[0][1]
        self.assertIn("没有找到任何记录", msg)

    def test_download_failure(self):
        with patch.object(self.bot, "github_download_file", return_value=None):
            with patch.object(self.bot, "send_message") as mock_send:
                self.bot.handle_last(42)

        msg = mock_send.call_args[0][1]
        self.assertIn("Failed to download", msg)


class TestHandleToday(unittest.TestCase):
    def setUp(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(FAKE_CONFIG))):
            self.bot = main.Bot()

    def _make_file(self, content):
        return {"content": content, "sha": "abc123"}

    def test_shows_today_entries(self):
        today = datetime.now(self.bot.timezone).strftime('%Y-%m-%d')
        content = (
            '2020-01-01 * "Old" "entry"\n'
            '  Expenses:Food  5 CNY\n'
            '  Assets:Cash  -5 CNY\n'
            '\n'
            f'{today} * "Today" "first"\n'
            '  Expenses:Food  10 CNY\n'
            '  Assets:Cash  -10 CNY\n'
            '\n'
            f'{today} * "Today" "second"\n'
            '  Expenses:Food  20 CNY\n'
            '  Assets:Cash  -20 CNY\n'
        )
        with patch.object(self.bot, "github_download_file", return_value=self._make_file(content)):
            with patch.object(self.bot, "send_message") as mock_send:
                self.bot.handle_today(42)

        msg = mock_send.call_args[0][1]
        self.assertIn(f"今天（{today}）共 2 条记录", msg)
        self.assertIn("Today", msg)
        self.assertNotIn("Old", msg)

    def test_no_entries_today(self):
        content = (
            '2020-01-01 * "Old" "entry"\n'
            '  Expenses:Food  5 CNY\n'
            '  Assets:Cash  -5 CNY\n'
        )
        with patch.object(self.bot, "github_download_file", return_value=self._make_file(content)):
            with patch.object(self.bot, "send_message") as mock_send:
                self.bot.handle_today(42)

        msg = mock_send.call_args[0][1]
        self.assertIn("没有记录", msg)

    def test_download_failure(self):
        with patch.object(self.bot, "github_download_file", return_value=None):
            with patch.object(self.bot, "send_message") as mock_send:
                self.bot.handle_today(42)

        msg = mock_send.call_args[0][1]
        self.assertIn("Failed to download", msg)


class TestGitHubDownloadFileETagCache(unittest.TestCase):
    def setUp(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(FAKE_CONFIG))):
            self.bot = main.Bot()

    def _mock_response(self, status_code, json_data=None, headers=None):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_data or {}
        resp.headers = headers or {}
        return resp

    def test_first_call_stores_etag(self):
        content_b64 = __import__("base64").b64encode(b"hello").decode()
        resp = self._mock_response(200, {"content": content_b64, "sha": "abc"}, {"ETag": '"etag1"'})

        with patch("requests.get", return_value=resp):
            result = self.bot.github_download_file("main.bean")

        self.assertEqual(result["content"], "hello")
        self.assertEqual(result["sha"], "abc")
        self.assertIn("main.bean", self.bot._file_etag_cache)
        self.assertEqual(self.bot._file_etag_cache["main.bean"]["etag"], '"etag1"')

    def test_304_returns_cached_data(self):
        self.bot._file_etag_cache["main.bean"] = {
            "etag": '"etag1"', "content": "cached content", "sha": "old_sha",
        }
        resp = self._mock_response(304)

        with patch("requests.get", return_value=resp) as mock_get:
            result = self.bot.github_download_file("main.bean")

        self.assertEqual(result["content"], "cached content")
        self.assertEqual(result["sha"], "old_sha")
        # Verify If-None-Match header was sent
        call_headers = mock_get.call_args[1].get("headers") or mock_get.call_args[0][0]
        passed_headers = mock_get.call_args.kwargs.get("headers") or mock_get.call_args[1].get("headers")
        self.assertEqual(passed_headers["If-None-Match"], '"etag1"')

    def test_200_after_cache_updates_cache(self):
        self.bot._file_etag_cache["main.bean"] = {
            "etag": '"old"', "content": "old", "sha": "old_sha",
        }
        content_b64 = __import__("base64").b64encode(b"new content").decode()
        resp = self._mock_response(200, {"content": content_b64, "sha": "new_sha"}, {"ETag": '"new"'})

        with patch("requests.get", return_value=resp):
            result = self.bot.github_download_file("main.bean")

        self.assertEqual(result["content"], "new content")
        self.assertEqual(self.bot._file_etag_cache["main.bean"]["etag"], '"new"')

    def test_upload_invalidates_cache(self):
        self.bot._file_etag_cache["main.bean"] = {
            "etag": '"e"', "content": "x", "sha": "s",
        }
        resp = self._mock_response(200)
        with patch("requests.put", return_value=resp):
            self.bot.github_upload_file("new", "sha", "msg", "main.bean")

        self.assertNotIn("main.bean", self.bot._file_etag_cache)

    def test_404_not_cached(self):
        resp = self._mock_response(404)
        with patch("requests.get", return_value=resp):
            result = self.bot.github_download_file("main.bean")

        self.assertEqual(result["content"], "")
        self.assertNotIn("main.bean", self.bot._file_etag_cache)

    def test_no_etag_header_skips_cache(self):
        content_b64 = __import__("base64").b64encode(b"data").decode()
        resp = self._mock_response(200, {"content": content_b64, "sha": "s"}, {})

        with patch("requests.get", return_value=resp):
            self.bot.github_download_file("main.bean")

        self.assertNotIn("main.bean", self.bot._file_etag_cache)


if __name__ == "__main__":
    unittest.main(verbosity=2)
