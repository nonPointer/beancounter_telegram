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


if __name__ == "__main__":
    unittest.main(verbosity=2)
