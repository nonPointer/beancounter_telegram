"""Fuzzing / edge-case tests for all parsing functions in main.py."""
import json
import unittest
from unittest.mock import patch

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


ACCOUNTS = [
    "Assets:Cash",
    "Assets:Bank:Chase",
    "Assets:Bank:Chase:Current",
    "Assets:WeChat:Current",
    "Assets:Alipay:Current",
    "Expenses:Food",
    "Expenses:Health",
    "Expenses:Transport",
    "Expenses:Others",
    "Income:Salary",
    "Liabilities:CreditCard:Chase",
    "Equity:OpenBalance",
]


def _bot():
    with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(FAKE_CONFIG))):
        return main.Bot()


# ═══════════════════════════════════════════════════════════════════
#  1. strip_code_fence
# ═══════════════════════════════════════════════════════════════════
class TestStripCodeFenceFuzz(unittest.TestCase):
    def setUp(self):
        self.bot = _bot()

    # --- basic passthrough ---
    def test_plain_entry_unchanged(self):
        entry = '2024-01-01 * "A" "B"\n  X:Y  1 USD\n  A:B  -1 USD'
        self.assertEqual(self.bot.strip_code_fence(entry), entry)

    def test_empty_string(self):
        self.assertEqual(self.bot.strip_code_fence(""), "")

    def test_whitespace_only(self):
        self.assertEqual(self.bot.strip_code_fence("   \n  \n  "), "")

    # --- full wrap ---
    def test_full_wrap_plain(self):
        inner = '2024-01-01 * "A" "B"\n  X:Y  1 USD\n  A:B  -1 USD'
        result = self.bot.strip_code_fence(f"```\n{inner}\n```")
        self.assertNotIn("```", result)
        self.assertIn("2024-01-01", result)

    def test_full_wrap_beancount(self):
        inner = '2024-01-01 * "A" "B"\n  X:Y  1 USD\n  A:B  -1 USD'
        result = self.bot.strip_code_fence(f"```beancount\n{inner}\n```")
        self.assertNotIn("```", result)
        self.assertIn("2024-01-01", result)

    # --- embedded fence with natural language (the recheck bug) ---
    def test_conversational_with_embedded_fence(self):
        raw = (
            '已将该笔理发支出计入 **Expenses:Health**：\n'
            '  ```beancount\n'
            '  2024-01-01 * "Barber" "理发"\n'
            '  ```\n'
            '  Assets:Cash  -13 GBP\n'
            '  Expenses:Health  13 GBP'
        )
        result = self.bot.strip_code_fence(raw)
        self.assertNotIn("```", result)
        self.assertNotIn("已将", result)
        self.assertNotIn("**", result)
        self.assertIn("2024-01-01", result)
        self.assertIn("Assets:Cash", result)

    def test_fence_after_explanation_paragraph(self):
        raw = (
            'Here is the corrected entry:\n\n'
            '```beancount\n'
            '2024-06-01 * "Shop" "Coffee"\n'
            '  Expenses:Food  5 USD\n'
            '  Assets:Cash  -5 USD\n'
            '```\n\n'
            'I changed the account as requested.'
        )
        result = self.bot.strip_code_fence(raw)
        self.assertNotIn("```", result)
        self.assertNotIn("corrected", result)
        self.assertNotIn("changed", result)
        self.assertIn("2024-06-01", result)
        self.assertIn("Expenses:Food", result)

    # --- leading ; comment preserved ---
    def test_leading_comment_preserved(self):
        raw = '; user input\n2024-01-01 * "A" "B"\n  X:Y  1 USD\n  A:B  -1 USD'
        result = self.bot.strip_code_fence(raw)
        self.assertIn("; user input", result)
        self.assertIn("2024-01-01", result)

    def test_leading_comment_inside_fence(self):
        raw = '```\n; user input\n2024-01-01 * "A" "B"\n  X:Y  1 USD\n  A:B  -1 USD\n```'
        result = self.bot.strip_code_fence(raw)
        self.assertIn("; user input", result)
        self.assertIn("2024-01-01", result)

    # --- no header found → fallback ---
    def test_no_header_returns_cleaned(self):
        raw = "just some random text\nwith multiple lines"
        result = self.bot.strip_code_fence(raw)
        self.assertIn("random text", result)

    # --- ! and txn flags ---
    def test_exclamation_flag(self):
        raw = '2024-01-01 ! "A" "B"\n  X:Y  1 USD\n  A:B  -1 USD'
        result = self.bot.strip_code_fence(raw)
        self.assertIn("2024-01-01 !", result)

    def test_txn_flag(self):
        raw = '2024-01-01 txn "A" "B"\n  X:Y  1 USD\n  A:B  -1 USD'
        result = self.bot.strip_code_fence(raw)
        self.assertIn("2024-01-01 txn", result)

    # --- multiple entries: only first extracted ---
    def test_multiple_entries_takes_first(self):
        raw = (
            '2024-01-01 * "A" "B"\n  X:Y  1 USD\n  A:B  -1 USD\n\n'
            '2024-01-02 * "C" "D"\n  X:Y  2 USD\n  A:B  -2 USD'
        )
        result = self.bot.strip_code_fence(raw)
        self.assertIn("2024-01-01", result)
        # Second entry should be excluded since it starts at column 0
        self.assertNotIn("2024-01-02", result)

    # --- entry with metadata lines ---
    def test_entry_with_metadata(self):
        raw = (
            '2024-01-01 * "A" "B"\n'
            '  datetime: "2024-01-01 12:00:00"\n'
            '  Expenses:Food  10 USD\n'
            '  Assets:Cash  -10 USD'
        )
        result = self.bot.strip_code_fence(raw)
        self.assertIn("datetime:", result)
        self.assertIn("Expenses:Food", result)


# ═══════════════════════════════════════════════════════════════════
#  2. normalize_and_validate_llm_entry
# ═══════════════════════════════════════════════════════════════════
class TestNormalizeAndValidateFuzz(unittest.TestCase):
    def setUp(self):
        self.bot = _bot()

    def _valid(self, header='2024-01-01 * "A" "B"', p1="Expenses:Food  10 USD", p2="Assets:Cash  -10 USD"):
        return f"{header}\n  {p1}\n  {p2}"

    # --- conversational LLM output ---
    def test_recheck_conversational_chinese(self):
        raw = (
            '已将支出计入 Health：\n'
            '  ```beancount\n'
            '  2024-01-01 * "Barber" "理发"\n'
            '  ```\n'
            '  Assets:Cash  -13 GBP\n'
            '  Expenses:Health  13 GBP'
        )
        result = self.bot.normalize_and_validate_llm_entry(raw, ACCOUNTS)
        self.assertIn("2024-01-01", result)
        self.assertNotIn("已将", result)
        self.assertNotIn("```", result)

    def test_recheck_english_explanation(self):
        raw = (
            'I updated the account to Health:\n\n'
            '```\n'
            '2024-03-01 * "Shop" "Coffee"\n'
            '  Expenses:Health  5 GBP\n'
            '  Assets:Cash  -5 GBP\n'
            '```'
        )
        result = self.bot.normalize_and_validate_llm_entry(raw, ACCOUNTS)
        self.assertNotIn("updated", result)
        self.assertIn("2024-03-01", result)

    # --- header validation ---
    def test_non_date_header_rejected(self):
        with self.assertRaises(ValueError):
            self.bot.normalize_and_validate_llm_entry(
                'Here is your entry:\n  Expenses:Food  10 USD\n  Assets:Cash  -10 USD',
                ACCOUNTS,
            )

    def test_invalid_date_format_rejected(self):
        with self.assertRaises(ValueError):
            self.bot.normalize_and_validate_llm_entry(
                '01-01-2024 * "A" "B"\n  Expenses:Food  10 USD\n  Assets:Cash  -10 USD',
                ACCOUNTS,
            )

    # --- amount edge cases ---
    def test_zero_amount_both_sides_rejected(self):
        with self.assertRaises(ValueError):
            self.bot.normalize_and_validate_llm_entry(
                self._valid(p1="Expenses:Food  0 USD", p2="Assets:Cash  0 USD"),
                ACCOUNTS,
            )

    def test_very_small_amounts(self):
        result = self.bot.normalize_and_validate_llm_entry(
            self._valid(p1="Expenses:Food  0.01 USD", p2="Assets:Cash  -0.01 USD"),
            ACCOUNTS,
        )
        self.assertIn("0.01", result)

    def test_large_amounts(self):
        result = self.bot.normalize_and_validate_llm_entry(
            self._valid(p1="Expenses:Food  999999.99 USD", p2="Assets:Cash  -999999.99 USD"),
            ACCOUNTS,
        )
        self.assertIn("999999.99", result)

    def test_negative_first_positive_second(self):
        result = self.bot.normalize_and_validate_llm_entry(
            self._valid(p1="Assets:Cash  -50 USD", p2="Expenses:Food  50 USD"),
            ACCOUNTS,
        )
        self.assertIn("Assets:Cash", result)
        self.assertIn("Expenses:Food", result)

    # --- cross currency ---
    def test_cross_currency_with_at_sign(self):
        entry = (
            '2024-01-01 * "A" "B"\n'
            '  Expenses:Food  100 CNY @ 0.14 USD\n'
            '  Assets:Cash  -14 USD'
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, ACCOUNTS)
        self.assertIn("@", result)

    def test_cross_currency_auto_fx_small_to_large(self):
        entry = (
            '2024-01-01 * "A" "B"\n'
            '  Expenses:Food  1 GBP\n'
            '  Assets:Cash  -100 JPY'
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, ACCOUNTS)
        self.assertIn("@ 100", result)

    def test_cross_currency_auto_fx_large_to_small(self):
        entry = (
            '2024-01-01 * "A" "B"\n'
            '  Expenses:Food  100 JPY\n'
            '  Assets:Cash  -1 GBP'
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, ACCOUNTS)
        self.assertIn("@ 100", result)

    def test_cross_currency_one_zero_rejected(self):
        """One side zero, other side non-zero in cross-currency → cannot infer FX."""
        with self.assertRaises(ValueError):
            self.bot.normalize_and_validate_llm_entry(
                '2024-01-01 * "A" "B"\n  Expenses:Food  10 USD\n  Assets:Cash  0 GBP',
                ACCOUNTS,
            )

    # --- three postings (split bill) ---
    def test_three_postings_balanced(self):
        entry = (
            '2024-01-01 * "Restaurant" "Dinner"\n'
            '  Assets:Cash  -90 USD\n'
            '  Assets:WeChat:Current  45 USD\n'
            '  Expenses:Food  45 USD'
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, ACCOUNTS)
        self.assertIn("Expenses:Food", result)

    def test_three_postings_unbalanced_rejected(self):
        entry = (
            '2024-01-01 * "Restaurant" "Dinner"\n'
            '  Assets:Cash  -90 USD\n'
            '  Assets:WeChat:Current  45 USD\n'
            '  Expenses:Food  44 USD'
        )
        with self.assertRaises(ValueError):
            self.bot.normalize_and_validate_llm_entry(entry, ACCOUNTS)

    # --- same sign rejected ---
    def test_both_positive_rejected(self):
        with self.assertRaises(ValueError):
            self.bot.normalize_and_validate_llm_entry(
                self._valid(p1="Expenses:Food  10 USD", p2="Assets:Cash  10 USD"),
                ACCOUNTS,
            )

    def test_both_negative_rejected(self):
        with self.assertRaises(ValueError):
            self.bot.normalize_and_validate_llm_entry(
                self._valid(p1="Expenses:Food  -10 USD", p2="Assets:Cash  -10 USD"),
                ACCOUNTS,
            )

    # --- too short ---
    def test_single_line_rejected(self):
        with self.assertRaises(ValueError):
            self.bot.normalize_and_validate_llm_entry('2024-01-01 * "A" "B"', ACCOUNTS)

    def test_two_lines_rejected(self):
        with self.assertRaises(ValueError):
            self.bot.normalize_and_validate_llm_entry(
                '2024-01-01 * "A" "B"\n  Expenses:Food  10 USD', ACCOUNTS
            )

    # --- metadata preservation ---
    def test_beancount_metadata_kept(self):
        entry = (
            '2024-01-01 * "A" "B"\n'
            '  category: "test"\n'
            '  Expenses:Food  10 USD\n'
            '  Assets:Cash  -10 USD'
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, ACCOUNTS)
        self.assertIn("category:", result)

    def test_natural_language_metadata_dropped(self):
        """Lines that look like natural language (not beancount metadata) should be dropped."""
        entry = (
            '2024-01-01 * "A" "B"\n'
            '  This is a description line\n'
            '  Expenses:Food  10 USD\n'
            '  Assets:Cash  -10 USD'
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, ACCOUNTS)
        self.assertNotIn("description line", result)

    def test_semicolon_comment_in_body_kept(self):
        entry = (
            '2024-01-01 * "A" "B"\n'
            '  ; this is a comment\n'
            '  Expenses:Food  10 USD\n'
            '  Assets:Cash  -10 USD'
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, ACCOUNTS)
        self.assertIn("; this is a comment", result)

    # --- prefer_current_account integration ---
    def test_auto_current_suffix(self):
        entry = (
            '2024-01-01 * "A" "B"\n'
            '  Expenses:Food  10 USD\n'
            '  Assets:WeChat  -10 USD'
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, ACCOUNTS)
        self.assertIn("Assets:WeChat:Current", result)

    def test_liabilities_no_current_suffix(self):
        entry = (
            '2024-01-01 * "A" "B"\n'
            '  Expenses:Food  10 USD\n'
            '  Liabilities:CreditCard:Chase  -10 USD'
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, ACCOUNTS)
        self.assertIn("Liabilities:CreditCard:Chase", result)
        self.assertNotIn(":Current", result)

    # --- posting with inline comment / rest ---
    def test_posting_with_inline_comment(self):
        entry = (
            '2024-01-01 * "A" "B"\n'
            '  Expenses:Food  10 USD ; lunch\n'
            '  Assets:Cash  -10 USD'
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, ACCOUNTS)
        self.assertIn("; lunch", result)

    def test_posting_with_at_cost(self):
        entry = (
            '2024-01-01 * "Broker" "Buy"\n'
            '  Assets:Cash  10 GOOGL @@ 3000 USD\n'
            '  Assets:Bank:Chase:Current  -3000 USD'
        )
        result = self.bot.normalize_and_validate_llm_entry(entry, ACCOUNTS)
        self.assertIn("@@", result)


# ═══════════════════════════════════════════════════════════════════
#  3. extract_all_directive_blocks
# ═══════════════════════════════════════════════════════════════════
class TestExtractAllDirectiveBlocksFuzz(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(main.extract_all_directive_blocks(""), [])

    def test_only_comments(self):
        self.assertEqual(main.extract_all_directive_blocks("; just a comment\n; another"), [])

    def test_only_blank_lines(self):
        self.assertEqual(main.extract_all_directive_blocks("\n\n\n"), [])

    def test_single_open_directive(self):
        content = "2024-01-01 open Assets:Cash USD"
        blocks = main.extract_all_directive_blocks(content)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0][0], "2024-01-01")

    def test_directive_with_trailing_blanks(self):
        content = '2024-01-01 * "A" "B"\n  Expenses:Food  10 USD\n  Assets:Cash  -10 USD\n\n\n'
        blocks = main.extract_all_directive_blocks(content)
        self.assertEqual(len(blocks), 1)
        # Trailing blanks should be trimmed from block text
        self.assertFalse(blocks[0][1].endswith("\n"))

    def test_adjacent_directives_no_blank_separator(self):
        content = (
            '2024-01-01 * "A" "B"\n'
            '  Expenses:Food  10 USD\n'
            '  Assets:Cash  -10 USD\n'
            '2024-01-02 * "C" "D"\n'
            '  Expenses:Food  20 USD\n'
            '  Assets:Cash  -20 USD'
        )
        blocks = main.extract_all_directive_blocks(content)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0][0], "2024-01-01")
        self.assertEqual(blocks[1][0], "2024-01-02")

    def test_comment_attached_to_correct_directive(self):
        content = (
            '; comment for first\n'
            '2024-01-01 * "A" "B"\n'
            '  X:Y  1 USD\n'
            '  A:B  -1 USD\n\n'
            '; comment for second\n'
            '2024-01-02 * "C" "D"\n'
            '  X:Y  2 USD\n'
            '  A:B  -2 USD'
        )
        blocks = main.extract_all_directive_blocks(content)
        self.assertEqual(len(blocks), 2)
        self.assertIn("; comment for first", blocks[0][1])
        self.assertNotIn("; comment for second", blocks[0][1])
        self.assertIn("; comment for second", blocks[1][1])

    def test_multiple_comment_lines(self):
        content = (
            '; line 1\n'
            '; line 2\n'
            '2024-01-01 * "A" "B"\n'
            '  X:Y  1 USD\n'
            '  A:B  -1 USD'
        )
        blocks = main.extract_all_directive_blocks(content)
        self.assertIn("; line 1", blocks[0][1])
        self.assertIn("; line 2", blocks[0][1])

    def test_blank_line_between_comment_and_directive(self):
        """Blank line separates comment from directive → comment not attached."""
        content = (
            '; orphan comment\n'
            '\n'
            '2024-01-01 * "A" "B"\n'
            '  X:Y  1 USD\n'
            '  A:B  -1 USD'
        )
        blocks = main.extract_all_directive_blocks(content)
        self.assertEqual(len(blocks), 1)
        # The blank line breaks the backward scan for comments
        self.assertNotIn("orphan comment", blocks[0][1])

    def test_directive_with_metadata(self):
        content = (
            '2024-01-01 * "A" "B"\n'
            '  datetime: "2024-01-01 12:00:00"\n'
            '  X:Y  1 USD\n'
            '  A:B  -1 USD'
        )
        blocks = main.extract_all_directive_blocks(content)
        self.assertEqual(len(blocks), 1)
        self.assertIn("datetime:", blocks[0][1])

    def test_mixed_directive_types(self):
        content = (
            '2024-01-01 open Assets:Cash USD\n\n'
            '2024-01-15 * "A" "B"\n'
            '  X:Y  1 USD\n'
            '  A:B  -1 USD\n\n'
            '2024-02-01 balance Assets:Cash 100 USD\n\n'
            '2024-03-01 close Assets:Cash'
        )
        blocks = main.extract_all_directive_blocks(content)
        self.assertEqual(len(blocks), 4)

    def test_tab_indented_postings(self):
        content = '2024-01-01 * "A" "B"\n\tX:Y  1 USD\n\tA:B  -1 USD'
        blocks = main.extract_all_directive_blocks(content)
        self.assertEqual(len(blocks), 1)
        self.assertIn("\tX:Y", blocks[0][1])


# ═══════════════════════════════════════════════════════════════════
#  4. extract_last_directive_block
# ═══════════════════════════════════════════════════════════════════
class TestExtractLastDirectiveBlockFuzz(unittest.TestCase):
    def test_empty(self):
        self.assertIsNone(main.extract_last_directive_block(""))

    def test_only_comments(self):
        self.assertIsNone(main.extract_last_directive_block("; comment\n; another"))

    def test_single_directive(self):
        content = '2024-01-01 * "A" "B"\n  X:Y  1 USD\n  A:B  -1 USD\n'
        result = main.extract_last_directive_block(content)
        self.assertIsNotNone(result)
        directive, new_content = result
        self.assertIn("2024-01-01", directive)
        # After removal, should be basically empty
        self.assertEqual(new_content.strip(), "")

    def test_removes_last_keeps_first(self):
        content = (
            '2024-01-01 * "A" "B"\n'
            '  X:Y  1 USD\n'
            '  A:B  -1 USD\n\n'
            '2024-01-02 * "C" "D"\n'
            '  X:Y  2 USD\n'
            '  A:B  -2 USD\n'
        )
        result = main.extract_last_directive_block(content)
        directive, new_content = result
        self.assertIn("2024-01-02", directive)
        self.assertIn("2024-01-01", new_content)
        self.assertNotIn("2024-01-02", new_content)

    def test_new_content_ends_with_newline(self):
        content = '2024-01-01 * "A" "B"\n  X:Y  1 USD\n  A:B  -1 USD\n'
        _, new_content = main.extract_last_directive_block(content)
        self.assertTrue(new_content.endswith("\n"))

    def test_directive_with_blank_lines_in_between(self):
        """Blank lines within a block (between postings) are part of the block."""
        content = (
            '2024-01-01 * "A" "B"\n'
            '  X:Y  1 USD\n'
            '\n'
            '  A:B  -1 USD\n'
        )
        result = main.extract_last_directive_block(content)
        directive, _ = result
        self.assertIn("X:Y", directive)
        self.assertIn("A:B", directive)

    def test_no_trailing_blank_separator(self):
        content = (
            '2024-01-01 * "A" "B"\n'
            '  X:Y  1 USD\n'
            '  A:B  -1 USD\n'
            '2024-01-02 * "C" "D"\n'
            '  X:Y  2 USD\n'
            '  A:B  -2 USD\n'
        )
        result = main.extract_last_directive_block(content)
        directive, new_content = result
        self.assertIn("2024-01-02", directive)


# ═══════════════════════════════════════════════════════════════════
#  5. ensure_datetime_metadata
# ═══════════════════════════════════════════════════════════════════
class TestEnsureDatetimeMetadataFuzz(unittest.TestCase):
    def setUp(self):
        self.bot = _bot()

    def test_empty_entry(self):
        self.assertEqual(self.bot.ensure_datetime_metadata("", "2024-01-01 12:00:00"), "")

    def test_no_header_inserts_after_first_line(self):
        """If no YYYY-MM-DD * header found, inserts after line 0."""
        entry = "some non-standard header\n  X:Y  1 USD"
        result = self.bot.ensure_datetime_metadata(entry, "2024-01-01 12:00:00")
        self.assertIn('datetime: "2024-01-01 12:00:00"', result)

    def test_already_present_not_duplicated(self):
        entry = (
            '2024-01-01 * "A" "B"\n'
            '  datetime: "2024-01-01 10:00:00"\n'
            '  X:Y  1 USD\n'
            '  A:B  -1 USD'
        )
        result = self.bot.ensure_datetime_metadata(entry, "2024-01-01 12:00:00")
        self.assertEqual(result.count("datetime:"), 1)
        # Original value preserved
        self.assertIn("10:00:00", result)

    def test_with_leading_comment(self):
        entry = (
            '; user input\n'
            '2024-01-01 * "A" "B"\n'
            '  X:Y  1 USD\n'
            '  A:B  -1 USD'
        )
        result = self.bot.ensure_datetime_metadata(entry, "2024-01-01 12:00:00")
        lines = result.splitlines()
        # Comment stays first
        self.assertEqual(lines[0], "; user input")
        # datetime after header
        self.assertIn("datetime:", lines[2])

    def test_single_line(self):
        entry = '2024-01-01 * "A" "B"'
        result = self.bot.ensure_datetime_metadata(entry, "2024-01-01 12:00:00")
        self.assertIn("datetime:", result)


# ═══════════════════════════════════════════════════════════════════
#  6. prepend_natural_language_comment
# ═══════════════════════════════════════════════════════════════════
class TestPrependNaturalLanguageCommentFuzz(unittest.TestCase):
    def setUp(self):
        self.bot = _bot()

    def test_empty_input(self):
        entry = '2024-01-01 * "A" "B"\n  X:Y  1 USD'
        self.assertEqual(self.bot.prepend_natural_language_comment(entry, ""), entry)

    def test_whitespace_only_input(self):
        entry = '2024-01-01 * "A" "B"\n  X:Y  1 USD'
        self.assertEqual(self.bot.prepend_natural_language_comment(entry, "   \n  "), entry)

    def test_multiline_input_flattened(self):
        entry = '2024-01-01 * "A" "B"\n  X:Y  1 USD'
        result = self.bot.prepend_natural_language_comment(entry, "line1\nline2\nline3")
        self.assertTrue(result.startswith("; line1 line2 line3\n"))

    def test_idempotent(self):
        entry = '; hello\n2024-01-01 * "A" "B"\n  X:Y  1 USD'
        result = self.bot.prepend_natural_language_comment(entry, "hello")
        self.assertEqual(result.count("; hello"), 1)

    def test_different_comment_adds_new(self):
        entry = '; hello\n2024-01-01 * "A" "B"\n  X:Y  1 USD'
        result = self.bot.prepend_natural_language_comment(entry, "world")
        self.assertTrue(result.startswith("; world\n"))

    def test_unicode_input(self):
        entry = '2024-01-01 * "A" "B"\n  X:Y  1 USD'
        result = self.bot.prepend_natural_language_comment(entry, "午餐 🍜 café")
        self.assertIn("; 午餐 🍜 café", result)


# ═══════════════════════════════════════════════════════════════════
#  7. prefer_current_account
# ═══════════════════════════════════════════════════════════════════
class TestPreferCurrentAccountFuzz(unittest.TestCase):
    def setUp(self):
        self.bot = _bot()

    def test_exact_match(self):
        self.assertEqual(
            self.bot.prefer_current_account("Assets:Cash", ACCOUNTS),
            "Assets:Cash",
        )

    def test_case_insensitive_exact(self):
        self.assertEqual(
            self.bot.prefer_current_account("assets:cash", ACCOUNTS),
            "Assets:Cash",
        )

    def test_adds_current(self):
        self.assertEqual(
            self.bot.prefer_current_account("Assets:WeChat", ACCOUNTS),
            "Assets:WeChat:Current",
        )

    def test_liabilities_no_current(self):
        self.assertEqual(
            self.bot.prefer_current_account("Liabilities:CreditCard:Chase", ACCOUNTS),
            "Liabilities:CreditCard:Chase",
        )

    def test_unknown_returned_as_is(self):
        self.assertEqual(
            self.bot.prefer_current_account("Assets:Unknown", ACCOUNTS),
            "Assets:Unknown",
        )

    def test_already_has_current(self):
        self.assertEqual(
            self.bot.prefer_current_account("Assets:WeChat:Current", ACCOUNTS),
            "Assets:WeChat:Current",
        )

    def test_equity_gets_current_if_exists(self):
        accounts_with_equity_current = ACCOUNTS + ["Equity:Opening:Current"]
        self.assertEqual(
            self.bot.prefer_current_account("Equity:Opening", accounts_with_equity_current),
            "Equity:Opening:Current",
        )

    def test_income_exact_match_preferred_over_current(self):
        """If the exact account exists, it's returned even if :Current variant also exists."""
        accounts_with = ACCOUNTS + ["Income:Salary:Current"]
        self.assertEqual(
            self.bot.prefer_current_account("Income:Salary", accounts_with),
            "Income:Salary",
        )

    def test_income_gets_current_when_no_exact(self):
        """If the exact account does NOT exist, :Current is tried."""
        accounts_without_exact = [a for a in ACCOUNTS if a != "Income:Salary"] + ["Income:Salary:Current"]
        self.assertEqual(
            self.bot.prefer_current_account("Income:Salary", accounts_without_exact),
            "Income:Salary:Current",
        )


# ═══════════════════════════════════════════════════════════════════
#  8. extract_accounts_from_entry
# ═══════════════════════════════════════════════════════════════════
class TestExtractAccountsFromEntryFuzz(unittest.TestCase):
    def setUp(self):
        self.bot = _bot()

    def test_normal(self):
        entry = '2024-01-01 * "A" "B"\n  Expenses:Food  10 USD\n  Assets:Cash  -10 USD'
        accounts = self.bot.extract_accounts_from_entry(entry)
        self.assertEqual(accounts, ["Expenses:Food", "Assets:Cash"])

    def test_with_metadata(self):
        entry = (
            '2024-01-01 * "A" "B"\n'
            '  datetime: "2024-01-01"\n'
            '  Expenses:Food  10 USD\n'
            '  Assets:Cash  -10 USD'
        )
        accounts = self.bot.extract_accounts_from_entry(entry)
        # datetime: line starts with spaces and has \S+ followed by space
        # so "datetime:" will be captured — this tests current behavior
        self.assertIn("Expenses:Food", accounts)
        self.assertIn("Assets:Cash", accounts)

    def test_no_postings(self):
        entry = '2024-01-01 * "A" "B"'
        accounts = self.bot.extract_accounts_from_entry(entry)
        self.assertEqual(accounts, [])

    def test_tab_indented(self):
        entry = '2024-01-01 * "A" "B"\n\tExpenses:Food  10 USD'
        accounts = self.bot.extract_accounts_from_entry(entry)
        self.assertEqual(accounts, ["Expenses:Food"])


# ═══════════════════════════════════════════════════════════════════
#  9. add_non_pnl_accounts_to_commit_message
# ═══════════════════════════════════════════════════════════════════
class TestAddNonPnlAccountsFuzz(unittest.TestCase):
    def setUp(self):
        self.bot = _bot()

    def test_only_expenses_income(self):
        entry = '2024-01-01 * "A" "B"\n  Expenses:Food  10 USD\n  Income:Salary  -10 USD'
        result = self.bot.add_non_pnl_accounts_to_commit_message("msg\n\n", entry)
        self.assertEqual(result, "msg\n\n")

    def test_assets_added(self):
        entry = '2024-01-01 * "A" "B"\n  Expenses:Food  10 USD\n  Assets:Cash  -10 USD'
        result = self.bot.add_non_pnl_accounts_to_commit_message("msg\n\n", entry)
        self.assertIn("Assets:Cash", result)
        self.assertNotIn("Expenses:Food", result)

    def test_liabilities_added(self):
        entry = '2024-01-01 * "A" "B"\n  Expenses:Food  10 USD\n  Liabilities:CreditCard:Chase  -10 USD'
        result = self.bot.add_non_pnl_accounts_to_commit_message("msg\n\n", entry)
        self.assertIn("Liabilities:CreditCard:Chase", result)

    def test_equity_added(self):
        entry = '2024-01-01 * "A" "B"\n  Expenses:Food  10 USD\n  Equity:OpenBalance  -10 USD'
        result = self.bot.add_non_pnl_accounts_to_commit_message("msg\n\n", entry)
        self.assertIn("Equity:OpenBalance", result)


# ═══════════════════════════════════════════════════════════════════
# 10. strip_code_fence + normalize combined edge cases
# ═══════════════════════════════════════════════════════════════════
class TestStripAndNormalizeCombined(unittest.TestCase):
    """End-to-end tests for LLM output cleanup."""
    def setUp(self):
        self.bot = _bot()

    def test_markdown_bold_in_explanation(self):
        raw = (
            '**Updated entry:**\n\n'
            '```\n'
            '2024-01-01 * "Shop" "Coffee"\n'
            '  Expenses:Food  5 USD\n'
            '  Assets:Cash  -5 USD\n'
            '```'
        )
        result = self.bot.normalize_and_validate_llm_entry(raw, ACCOUNTS)
        self.assertNotIn("**", result)
        self.assertNotIn("Updated", result)
        self.assertIn("2024-01-01", result)

    def test_numbered_list_explanation(self):
        raw = (
            'Changes made:\n'
            '1. Changed account to Health\n'
            '2. Updated amount\n\n'
            '```beancount\n'
            '2024-01-01 * "A" "B"\n'
            '  Expenses:Health  15 USD\n'
            '  Assets:Cash  -15 USD\n'
            '```'
        )
        result = self.bot.normalize_and_validate_llm_entry(raw, ACCOUNTS)
        self.assertNotIn("Changes", result)
        self.assertIn("Expenses:Health", result)

    def test_double_fenced_blocks(self):
        """Two code blocks; should extract the first valid entry."""
        raw = (
            '```\n'
            '2024-01-01 * "A" "B"\n'
            '  Expenses:Food  10 USD\n'
            '  Assets:Cash  -10 USD\n'
            '```\n\n'
            '```\n'
            '2024-02-01 * "C" "D"\n'
            '  Expenses:Food  20 USD\n'
            '  Assets:Cash  -20 USD\n'
            '```'
        )
        result = self.bot.normalize_and_validate_llm_entry(raw, ACCOUNTS)
        self.assertIn("2024-01-01", result)
        # Second entry is not part of the first block
        self.assertNotIn("2024-02-01", result)

    def test_pure_entry_with_extra_whitespace(self):
        raw = '\n\n  2024-01-01 * "A" "B"\n    Expenses:Food  10 USD\n    Assets:Cash  -10 USD\n\n'
        result = self.bot.normalize_and_validate_llm_entry(raw, ACCOUNTS)
        self.assertIn("2024-01-01", result)

    def test_entry_with_only_comment_no_postings_rejected(self):
        raw = '; comment\n2024-01-01 * "A" "B"\n  ; only a comment'
        with self.assertRaises(ValueError):
            self.bot.normalize_and_validate_llm_entry(raw, ACCOUNTS)


# ═══════════════════════════════════════════════════════════════════
# 11. FX rate edge cases in normalize_and_validate
# ═══════════════════════════════════════════════════════════════════
class TestFXRateEdgeCases(unittest.TestCase):
    def setUp(self):
        self.bot = _bot()

    def test_equal_amounts_different_currencies(self):
        """1 GBP and -1 USD → rate = 1."""
        entry = '2024-01-01 * "A" "B"\n  Expenses:Food  1 GBP\n  Assets:Cash  -1 USD'
        result = self.bot.normalize_and_validate_llm_entry(entry, ACCOUNTS)
        self.assertIn("@ 1 ", result)

    def test_fractional_rate(self):
        """7.5 CNY and -1 USD → rate = 7.5."""
        entry = '2024-01-01 * "A" "B"\n  Expenses:Food  7.5 CNY\n  Assets:Cash  -1 USD'
        result = self.bot.normalize_and_validate_llm_entry(entry, ACCOUNTS)
        self.assertIn("@ 7.5 CNY", result)

    def test_existing_at_not_doubled(self):
        """If @ already present, don't add another."""
        entry = '2024-01-01 * "A" "B"\n  Expenses:Food  7.5 CNY @ 0.14 USD\n  Assets:Cash  -1.05 USD'
        result = self.bot.normalize_and_validate_llm_entry(entry, ACCOUNTS)
        self.assertEqual(result.count("@"), 1)

    def test_existing_double_at_not_modified(self):
        entry = '2024-01-01 * "A" "B"\n  Expenses:Food  10 GOOGL @@ 3000 USD\n  Assets:Cash  -3000 USD'
        result = self.bot.normalize_and_validate_llm_entry(entry, ACCOUNTS)
        self.assertIn("@@", result)

    def test_rate_precision_not_excessive(self):
        """Rate like 1/3 should not have excessive trailing zeros."""
        entry = '2024-01-01 * "A" "B"\n  Expenses:Food  3 GBP\n  Assets:Cash  -1 USD'
        result = self.bot.normalize_and_validate_llm_entry(entry, ACCOUNTS)
        # Should contain @ with rate, not have trailing zeros
        # 3/1 = 3, or 1/3 ≈ 0.33333333 → should be trimmed
        self.assertNotIn(".00000000", result)


if __name__ == "__main__":
    unittest.main()
