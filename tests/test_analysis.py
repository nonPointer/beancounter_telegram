"""Synthetic protocol and real BQL tests; no configured services or private data."""

from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from beancounter.analysis import CAPABILITY_TTL, render_analysis
from beancounter.analysis_query import QuerySession, encode_value, query_result, validate_query
from beancounter.bot import Bot
from beancounter.bot_utils import _utf16_len
from beancounter.ledger_validation import check_ledger
from beancounter.settings import Settings
from test_bot import MOCK_CONFIG
from test_reports import ACCOUNTS, HISTORY


QUERY = 'SELECT account, sum(position) WHERE account ~ "^Expenses:" AND year = 2026 AND month = 9 GROUP BY account ORDER BY account'
QUERY_JSON = {"content": json.dumps({"queries": [{"bql": QUERY}]})}
FINAL = {"content": json.dumps({"answer": "本月餐饮存在退款 [q1]。", "evidence_ids": ["q1"]})}


def tool_message(name="query_ledger", arguments=None, call_id="call_1"):
    return {"content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps({"bql": QUERY} if arguments is None else arguments),
    }}]}


def http_error(status=400, text="tools is not supported"):
    response = requests.Response()
    response.status_code = status
    response._content = text.encode()
    return requests.HTTPError("synthetic failure", response=response)


class InlineSession:
    """Loop tests use real BQL without spawning; separate tests exercise the worker."""

    def __init__(self, loaded):
        self.loaded = loaded

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def query(self, bql, deadline):
        return query_result(self.loaded, bql)


@contextmanager
def fake_api(replies):
    """Real loopback HTTP verifies wire payloads, including a native tool-result round trip."""
    received = []
    pending = iter(replies)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            reply = next(pending, {"content": "unexpected request"})
            status, body = reply if isinstance(reply, tuple) else (200, {"choices": [{"message": reply}]})
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class AnalysisFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loaded = check_ledger({"main.bean": ACCOUNTS + HISTORY}, "main.bean")

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="beancounter-analysis-test-")
        self.addCleanup(directory.cleanup)
        settings = Settings({**MOCK_CONFIG, "LLM_BACKENDS": [{
            "LLM_API_BASE_URL": "https://llm.invalid/v1", "LLM_API_KEY": "synthetic-key", "LLM_MODEL": "test-model",
        }]}, base_dir=Path(directory.name))
        self.bot = Bot(settings=settings, state_path=":memory:")
        self.addCleanup(self.bot.close)
        self.bot.load_ledger = MagicMock(return_value=self.loaded)
        self.bot._accounts_for_prompt = MagicMock(return_value=["Assets:Cash", "Expenses:Food:Cafe"])
        self.backend = settings.LLM_BACKENDS[0]
        self.deadline = time.monotonic() + settings.ANALYSIS_TIMEOUT_SECONDS

    def run_analysis(self, replies, *, native=False):
        self.bot._supports_tools = MagicMock(return_value=native)
        self.bot._request_llm_message = MagicMock(side_effect=replies)
        with patch("beancounter.analysis.QuerySession", InlineSession):
            return self.bot.analyze_ledger("分析本月开支", "2026-09-10")


class TestCapability(AnalysisFixture):
    def test_native_round_trip_and_cache_without_private_context(self):
        request = self.bot._request_llm_message = MagicMock(side_effect=[tool_message("capability_probe", {}), {"content": "OK"}])
        with patch.object(self.bot, "_with_user_preferences", side_effect=AssertionError("probe must not read user.md")):
            self.assertTrue(self.bot._supports_tools(self.backend, self.deadline))
            self.assertTrue(self.bot._supports_tools(self.backend, self.deadline))
        self.assertEqual(request.call_count, 2)
        data = request.call_args.args[1]
        self.assertTrue(any(m["role"] == "tool" for m in data["messages"]))
        self.assertNotIn("Assets", json.dumps(data))
        self.bot.load_ledger.assert_not_called()

    def test_explicit_rejection_cached(self):
        self.bot._request_llm_message = MagicMock(side_effect=http_error())
        self.assertFalse(self.bot._supports_tools(self.backend, self.deadline))
        self.assertFalse(self.bot._supports_tools(self.backend, self.deadline))
        self.bot._request_llm_message.assert_called_once()

    def test_tool_result_rejection_is_detected(self):
        self.bot._request_llm_message = MagicMock(side_effect=[tool_message("capability_probe", {}), http_error(400, "role 'tool' not supported")])
        self.assertFalse(self.bot._supports_tools(self.backend, self.deadline))
        self.assertFalse(self.bot._supports_tools(self.backend, self.deadline))
        self.assertEqual(self.bot._request_llm_message.call_count, 2)

    def test_text_only_probe_is_inconclusive_not_cached(self):
        self.bot._request_llm_message = MagicMock(return_value={"content": "I cannot call tools"})
        self.assertFalse(self.bot._supports_tools(self.backend, self.deadline))
        self.assertFalse(self.bot._supports_tools(self.backend, self.deadline))
        self.assertEqual(self.bot._request_llm_message.call_count, 2)
        self.assertEqual(self.bot._tool_capabilities, {})

    def test_transient_and_unrelated_errors_are_not_negative_evidence(self):
        for error in [http_error(401), http_error(403), http_error(429), http_error(500), requests.Timeout(), http_error(400, "invalid model")]:
            with self.subTest(error=repr(error)):
                self.bot._request_llm_message = MagicMock(side_effect=error)
                with self.assertRaises(type(error)):
                    self.bot._supports_tools(self.backend, self.deadline)
                self.assertEqual(self.bot._tool_capabilities, {})

    def test_cache_expiry_and_model_credential_scope(self):
        self.bot._cache_tool_support(self.backend, False)
        request = self.bot._request_llm_message = MagicMock(return_value={"content": "plain"})
        self.assertFalse(self.bot._supports_tools({**self.backend, "model": "other"}, self.deadline))
        self.assertFalse(self.bot._supports_tools({**self.backend, "api_key": "other-key"}, self.deadline))
        now = time.monotonic()
        with patch("beancounter.analysis.time.monotonic", return_value=now + CAPABILITY_TTL + 1):
            self.assertFalse(self.bot._supports_tools(self.backend, now + CAPABILITY_TTL + 100))
        self.assertEqual(request.call_count, 3)

    def test_probe_cannot_run_arbitrary_tool(self):
        self.bot._request_llm_message = MagicMock(return_value=tool_message("query_ledger"))
        self.assertFalse(self.bot._supports_tools(self.backend, self.deadline))
        self.assertEqual(self.bot._tool_capabilities, {})

    def test_malformed_probe_call_is_inconclusive(self):
        for message in [{"tool_calls": "not a list"}, tool_message("capability_probe", [])]:
            self.bot._request_llm_message = MagicMock(return_value=message)
            self.assertFalse(self.bot._supports_tools(self.backend, self.deadline))
            self.assertEqual(self.bot._tool_capabilities, {})


class TestQueryWorker(AnalysisFixture):
    def test_select_aliases_and_decimal_currency_fidelity(self):
        bql = 'SELECT root(account, 2) AS category, cost(sum(position)) AS total WHERE account ~ "^Expenses:" AND month = 9 GROUP BY category ORDER BY category'
        result, table = query_result(self.loaded, bql)
        self.assertFalse(result["truncated"])
        encoded = json.dumps(result)
        self.assertIn('"currency": "USD"', encoded)
        self.assertIn('"number": "18"', encoded)
        self.assertIn("Food", table)

    def test_readonly_and_metadata_restrictions(self):
        for bql in ["PRINT", "BALANCES", "SELECT *", "SELECT filename", 'SELECT meta("prompt")', 'SELECT entry_meta("prompt")', "SELECT account FROM year = 2026", "SELECT account; PRINT", "SELECT filename AS filename ORDER BY filename", "DROP TABLE entries"]:
            with self.subTest(bql=bql), self.assertRaises(Exception):
                validate_query(bql)

    def test_bad_or_oversized_query(self):
        for bql in [None, {}, "", "SELECT " + "x" * 4000]:
            with self.assertRaises(ValueError):
                validate_query(bql)

    def test_typed_values_and_duplicate_columns(self):
        self.assertEqual(encode_value(Decimal("0.12345678901234567890")), {"decimal": "0.12345678901234567890"})
        self.assertEqual(encode_value(date(2026, 9, 10)), {"date": "2026-09-10"})
        with self.assertRaises(ValueError):
            encode_value({"prompt": "not for the model"})
        result, _ = query_result(self.loaded, "SELECT date, date LIMIT 1")
        self.assertEqual(len(result["columns"]), 2)
        self.assertEqual(len(result["rows"][0]), 2)

    def test_shares_and_liabilities_preserve_units_sign(self):
        result, _ = query_result(self.loaded, 'SELECT account, sum(position) WHERE account ~ "^Assets:Shares|^Liabilities:" GROUP BY account')
        text = json.dumps(result)
        self.assertIn('"number": "0.123456"', text)
        self.assertIn('"currency": "DEMO"', text)
        self.assertIn('"number": "-18"', text)

    def test_cross_query_decimal_comparison_runs_in_bql(self):
        result, _ = query_result(self.loaded, 'SELECT 18.00 - 9.00 AS difference, (18.00 - 9.00) / 9.00 * 100 AS percent, "GBP" AS currency LIMIT 1')
        difference, percent, currency = result["rows"][0]
        self.assertEqual(Decimal(difference["decimal"]), Decimal("9.00"))
        self.assertEqual(Decimal(percent["decimal"]), Decimal("100"))
        self.assertEqual(currency, "GBP")

    def test_result_row_cell_and_size_limits(self):
        with patch("beancounter.analysis_query.beancount_query.run_query", return_value=([("payee", str)], [("店" * 900,)] * 110)):
            result, table = query_result(self.loaded, "SELECT payee")
        self.assertEqual(result["total_rows"], 110)
        self.assertEqual(len(result["rows"]), 100)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["rows"][0][0], {"truncated": True})
        self.assertIn("不完整", table)
        self.assertLess(len(json.dumps(result, ensure_ascii=False)), 16000)
        with patch("beancounter.analysis_query.beancount_query.run_query", return_value=([("payee", str)], [("x" * 700,)] * 100)):
            result, _ = query_result(self.loaded, "SELECT payee")
        self.assertLess(len(result["rows"]), 100)
        self.assertTrue(result["truncated"])

    def test_no_rows_is_a_successful_result(self):
        result, table = query_result(self.loaded, "SELECT account WHERE year = 1900")
        self.assertEqual(result["rows"], [])
        self.assertFalse(result["truncated"])
        self.assertIn("没有找到", table)

    def test_spawn_worker_reuses_snapshot_without_mutating_cache(self):
        before = repr(self.loaded)
        with QuerySession(self.loaded) as session:
            pid = session.process.pid
            first, _ = session.query(QUERY, time.monotonic() + 20)
            second, _ = session.query(QUERY, time.monotonic() + 20)
            self.assertEqual(first, second)
            self.assertEqual(pid, session.process.pid)
        self.assertNotIn(pid, [p.pid for p in multiprocessing.active_children()])
        self.assertEqual(repr(self.loaded), before)
        self.bot.load_ledger.assert_not_called()

    def test_worker_rejects_metadata_and_can_retry(self):
        with QuerySession(self.loaded) as session:
            with self.assertRaises(ValueError):
                session.query('SELECT meta("prompt")', time.monotonic() + 20)
            result, _ = session.query(QUERY, time.monotonic() + 20)
            self.assertTrue(result["rows"])

    def test_worker_timeout_cleans_up(self):
        with QuerySession(self.loaded) as session:
            pid = session.process.pid
            with patch.object(session.connection, "poll", return_value=False), self.assertRaises(TimeoutError):
                session.query(QUERY, time.monotonic() + 20)
        self.assertNotIn(pid, [p.pid for p in multiprocessing.active_children()])


class TestAnalysisLoop(AnalysisFixture):
    def test_json_two_queries_and_final_only_loads_once(self):
        replies = [QUERY_JSON, {"content": json.dumps({"queries": [{"bql": 'SELECT count(date) WHERE month = 8'}]})}, FINAL]
        result = self.run_analysis(replies)
        self.assertEqual(len(result), 2)
        self.assertIn("退款", result[0])
        self.assertIn("<pre><code>", result[1])
        self.bot.load_ledger.assert_called_once()
        self.assertFalse(self.bot.pending_llm_entries)
        for call in self.bot._request_llm_message.call_args_list:
            self.assertNotIn("tools", call.args[1])

    def test_native_tool_results_returned_to_model(self):
        self.run_analysis([tool_message(), FINAL], native=True)
        payload = self.bot._request_llm_message.call_args.args[1]
        self.assertEqual(payload["tools"][0]["function"]["name"], "query_ledger")
        results = [m for m in payload["messages"] if m["role"] == "tool"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["tool_call_id"], "call_1")
        self.assertEqual(json.loads(results[0]["content"])["id"], "q1")

    def test_query_errors_returned_and_repaired(self):
        bad = {"content": json.dumps({"queries": [{"bql": "SELECT filename"}]})}
        final = {"content": json.dumps({"answer": "修正后查询 [q2]", "evidence_ids": ["q2"]})}
        result = self.run_analysis([bad, QUERY_JSON, final])
        self.assertIn("[q2]", result[0])
        second = self.bot._request_llm_message.call_args_list[1].args[1]
        self.assertIn("Column not allowed", json.dumps(second))

    def test_invalid_answers_do_not_invent_evidence(self):
        result = self.run_analysis([FINAL] * 5)
        self.assertIn("未取得可用", result[0])
        self.assertEqual(self.bot._request_llm_message.call_count, 5)

    def test_missing_or_unknown_citation_rejected_after_success(self):
        for ids in [[], ["q99"], [None], "q1"]:
            with self.subTest(ids=ids):
                bad = {"content": json.dumps({"answer": "Unsupported answer", "evidence_ids": ids})}
                result = self.run_analysis([QUERY_JSON, *([bad] * 4)])
                self.assertNotIn("Unsupported answer", result[0])
                self.assertIn("分析未完成", result[0])

    def test_duplicate_tool_ids_rejected(self):
        reply = tool_message()
        reply["tool_calls"] *= 2
        with patch.object(InlineSession, "query") as query:
            self.run_analysis([reply] * 5, native=True)
            query.assert_not_called()

    def test_preferences_reloaded_each_analysis_step(self):
        path = MagicMock()
        path.exists.return_value = True
        path.read_text.side_effect = ["first preference <!-- private comment -->", "second preference"]
        self.bot.settings.USER_PROMPT_PATH = path
        self.run_analysis([QUERY_JSON, FINAL])
        calls = self.bot._request_llm_message.call_args_list
        first, second = (json.dumps(call.args[1], ensure_ascii=False) for call in calls)
        self.assertIn("first preference", first)
        self.assertNotIn("private comment", first)
        self.assertIn("second preference", second)
        self.assertNotIn("first preference", second)

    def test_unknown_tools_and_malformed_calls_never_execute(self):
        for reply in [tool_message("write_ledger"), tool_message(arguments={"bql": QUERY, "write": True}), {"tool_calls": "bad"}, {"content": '{"queries":[{"bql":null}]}'}]:
            with self.subTest(reply=reply), patch.object(InlineSession, "query") as query:
                self.run_analysis([reply] * 5, native=True)
                query.assert_not_called()

    def test_query_count_and_round_budgets(self):
        batch = {"content": json.dumps({"queries": [{"bql": QUERY}] * 8})}
        self.run_analysis([batch, QUERY_JSON, FINAL])
        self.assertEqual(self.bot._request_llm_message.call_count, 2)
        self.run_analysis([QUERY_JSON] * 8)
        self.assertEqual(self.bot._request_llm_message.call_count, 5)

    def test_oversized_batch_rejected_without_execution(self):
        batch = {"content": json.dumps({"queries": [{"bql": QUERY}] * 9})}
        with patch.object(InlineSession, "query") as query:
            self.run_analysis([batch] * 5)
            query.assert_not_called()

    def test_actual_tool_rejection_switches_to_json(self):
        result = self.run_analysis([http_error(), QUERY_JSON, FINAL], native=True)
        self.assertIn("退款", result[0])
        calls = self.bot._request_llm_message.call_args_list
        self.assertIn("tools", calls[0].args[1])
        self.assertNotIn("tools", calls[1].args[1])
        self.assertTrue(any(not value[0] for value in self.bot._tool_capabilities.values()))

    def test_backend_failure_preserves_query_evidence_and_budget(self):
        self.bot.settings.LLM_BACKENDS.append({**self.backend, "model": "backup"})
        result = self.run_analysis([QUERY_JSON, requests.Timeout(), FINAL])
        self.assertIn("退款", result[0])
        self.assertEqual(self.bot._request_llm_message.call_args.args[0]["model"], "backup")
        self.assertIn('q1', json.dumps(self.bot._request_llm_message.call_args.args[1]))
        self.bot.load_ledger.assert_called_once()

    def test_probe_failure_moves_to_next_backend_without_negative_cache(self):
        self.bot.settings.LLM_BACKENDS.append({**self.backend, "model": "backup"})
        self.bot._request_llm_message = MagicMock(side_effect=[http_error(401), http_error(), QUERY_JSON, FINAL])
        with patch("beancounter.analysis.QuerySession", InlineSession):
            result = self.bot.analyze_ledger("分析开支", "2026-09-10")
        self.assertIn("退款", result[0])
        self.assertEqual(len(self.bot._tool_capabilities), 1)
        self.assertEqual(next(iter(self.bot._tool_capabilities))[1], "backup")

    def test_timeout_returns_partial_results(self):
        original = InlineSession.query
        count = 0

        def query(session, bql, deadline):
            nonlocal count
            count += 1
            if count == 2:
                raise TimeoutError()
            return original(session, bql, deadline)

        with patch.object(InlineSession, "query", query):
            result = self.run_analysis([QUERY_JSON, QUERY_JSON])
        self.assertIn("超时", result[0])
        self.assertEqual(len(result), 2)

    def test_expired_budget_and_response_size(self):
        self.bot._request_llm_message = MagicMock()
        with self.assertRaises(TimeoutError):
            self.bot._analysis_request(self.backend, {}, time.monotonic() - 1)
        self.bot._request_llm_message.assert_not_called()
        self.bot._request_llm_message.return_value = {"content": "x" * 33000}
        with self.assertRaises(ValueError):
            self.bot._analysis_request(self.backend, {}, self.deadline)

    def test_rendering_escapes_html_and_caps_utf16(self):
        result, table = query_result(self.loaded, QUERY)
        evidence = [{"id": "q1", "bql": QUERY, "result": result, "table": table + "😀" * 5000}]
        messages = render_analysis("<script>" + "😀" * 5000, evidence, ["q1"])
        self.assertIn("&lt;script&gt;", messages[0])
        self.assertIn("已截断", messages[0])
        self.assertIn("展示已截断", messages[1])
        for message in messages:
            self.assertLessEqual(_utf16_len(html.unescape(message)), 4096)
            self.assertLessEqual(_utf16_len(message), 4096)
        messages = render_analysis('"<&' * 5000, [], [])
        self.assertLessEqual(_utf16_len(messages[0]), 4096)

    def test_analysis_route_and_handler_never_create_draft(self):
        self.bot._call_llm_backends = MagicMock(return_value='{"intent":"analysis"}')
        self.assertEqual(self.bot.route_intent("分析消费", "2026-09-10"), {"intent": "analysis"})
        self.bot.parse_accounts = MagicMock(return_value=["Assets:Cash"])
        self.bot.analyze_ledger = MagicMock(return_value=["分析", "<pre><code>结果</code></pre>"])
        self.bot.send_message = MagicMock()
        self.bot.publish_llm_draft = MagicMock()
        self.bot.handle_natural_language(123, "分析消费", "分析消费", "2026-09-10")
        self.assertEqual(self.bot.send_message.call_count, 2)
        self.bot.publish_llm_draft.assert_not_called()

    def test_simple_query_does_not_probe(self):
        self.bot.parse_accounts = MagicMock(return_value=["Assets:Cash"])
        self.bot.route_intent = MagicMock(return_value={"intent": "query", "bql": QUERY})
        self.bot.answer_query = MagicMock(return_value=(QUERY, "result"))
        self.bot.send_message = MagicMock()
        self.bot._supports_tools = MagicMock()
        self.bot.handle_natural_language(123, "查询余额", "查询余额", "2026-09-10")
        self.bot._supports_tools.assert_not_called()


class TestAnalysisTimeouts(AnalysisFixture):
    def test_timeout_defaults_overrides_and_invalid_values(self):
        defaults = {"ANALYSIS_REQUEST_TIMEOUT_SECONDS": 180, "ANALYSIS_TIMEOUT_SECONDS": 600, "ANALYSIS_PROBE_BUDGET_SECONDS": 120}
        for name, default in defaults.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(self.bot.settings, name), default)
                self.assertEqual(getattr(Settings({**MOCK_CONFIG, name: "240"}), name), 240)
                for invalid in (0, -1, None, "invalid"):
                    self.assertEqual(getattr(Settings({**MOCK_CONFIG, name: invalid}), name), default)

    def test_request_timeout_is_configurable_and_clamped_to_remaining_budget(self):
        self.bot._request_llm_message = MagicMock(return_value=FINAL)
        with patch("beancounter.analysis.time.monotonic", return_value=100):
            self.bot._analysis_request(self.backend, {}, 700)
            self.assertEqual(self.bot._request_llm_message.call_args.kwargs["timeout"], 180)
            self.bot.settings.ANALYSIS_REQUEST_TIMEOUT_SECONDS = 240
            self.bot._analysis_request(self.backend, {}, 700)
            self.assertEqual(self.bot._request_llm_message.call_args.kwargs["timeout"], 240)
            self.bot._analysis_request(self.backend, {}, 107)
            self.assertEqual(self.bot._request_llm_message.call_args.kwargs["timeout"], 7)

    def test_slow_probe_and_analysis_can_finish_beyond_old_limits(self):
        self.bot.settings.LLM_BACKENDS.append({**self.backend, "model": "backup"})
        self.bot._cache_tool_support(self.backend, True)
        now, timeouts = [0], []
        replies = iter([
            (1, tool_message(call_id="one")), (1, tool_message(call_id="two")),
            (1, http_error(429)), (54, tool_message("capability_probe", {})),
            (55, {"content": "OK"}), (90, FINAL),
        ])

        def request(backend, payload, purpose, *, timeout):
            duration, reply = next(replies)
            timeouts.append(timeout)
            self.assertLess(duration, timeout)
            now[0] += duration
            if isinstance(reply, Exception):
                raise reply
            return reply

        with patch("beancounter.analysis.QuerySession", InlineSession), patch("beancounter.analysis.time.monotonic", side_effect=lambda: now[0]), patch.object(self.bot, "_request_llm_message", side_effect=request):
            result = self.bot.analyze_ledger("分析本月开支", "2026-09-10")
        self.assertIn("退款", result[0])
        self.assertEqual(now[0], 202)
        self.assertEqual(timeouts, [180, 180, 180, 120, 66, 180])
        self.bot.load_ledger.assert_called_once()

    def test_probe_budget_shared_across_backends_and_exhaustion_uses_json(self):
        self.bot.settings.LLM_BACKENDS += [{**self.backend, "model": "backup"}, {**self.backend, "model": "last"}]
        now, seen = [0], []
        replies = iter([
            (80, requests.ConnectionError()), (30, tool_message("capability_probe", {})),
            (10, requests.ReadTimeout()), (1, requests.ConnectionError()),
            (1, QUERY_JSON), (1, FINAL),
        ])

        def request(backend, payload, purpose, *, timeout):
            seen.append((backend["model"], timeout, "tools" in payload))
            duration, reply = next(replies)
            now[0] += duration
            if isinstance(reply, Exception):
                raise reply
            return reply

        with patch("beancounter.analysis.QuerySession", InlineSession), patch("beancounter.analysis.time.monotonic", side_effect=lambda: now[0]), patch.object(self.bot, "_request_llm_message", side_effect=request):
            result = self.bot.analyze_ledger("分析本月开支", "2026-09-10")
        self.assertIn("退款", result[0])
        self.assertEqual([s[1] for s in seen[:3]], [120, 40, 10])
        self.assertEqual(seen[-2:], [("last", 180, False), ("last", 180, False)])
        self.assertEqual(self.bot._tool_capabilities, {})

    def test_cached_support_still_works_with_expired_probe_deadline(self):
        self.bot._cache_tool_support(self.backend, True)
        with patch.object(self.bot, "_request_llm_message") as request:
            self.assertTrue(self.bot._supports_tools(self.backend, time.monotonic() - 1))
            request.assert_not_called()

    def test_probe_timeout_falls_back_without_negative_cache(self):
        self.bot._request_llm_message = MagicMock(side_effect=[requests.ReadTimeout(), QUERY_JSON, FINAL])
        with patch("beancounter.analysis.QuerySession", InlineSession):
            result = self.bot.analyze_ledger("分析开支", "2026-09-10")
        self.assertIn("退款", result[0])
        calls = self.bot._request_llm_message.call_args_list
        self.assertIn("tools", calls[0].args[1])
        self.assertNotIn("tools", calls[1].args[1])
        self.assertEqual(self.bot._tool_capabilities, {})

    def test_custom_workflow_deadline_stops_late_responses(self):
        self.bot.settings.ANALYSIS_TIMEOUT_SECONDS = 12
        self.bot._cache_tool_support(self.backend, True)
        now, timeouts = [0], []

        def request(backend, payload, purpose, *, timeout):
            timeouts.append(timeout)
            now[0] += 13
            return FINAL

        with patch("beancounter.analysis.QuerySession", InlineSession), patch("beancounter.analysis.time.monotonic", side_effect=lambda: now[0]), patch.object(self.bot, "_request_llm_message", side_effect=request):
            result = self.bot.analyze_ledger("分析开支", "2026-09-10")
        self.assertEqual(timeouts, [12])
        self.assertIn("分析未完成", result[0])

    def test_http_diagnostics_retain_status_mask_secrets_and_omit_failed_generation(self):
        message = "quota exceeded " + " ".join([self.backend["api_key"], self.bot.settings.GITHUB_TOKEN, self.bot.settings.TELEGRAM_BOT_TOKEN]) + "\nAuthorization: Bearer unconfigured-secret"
        error = http_error(429, json.dumps({"error": {
            "type": "tokens", "code": "rate_limit", "message": message,
            "failed_generation": "synthetic ledger echo must be omitted",
        }}))
        detail = self.bot._analysis_error_detail(error)
        for expected in ("HTTP 429", "type=tokens", "code=rate_limit", "quota exceeded", "[REDACTED]"):
            self.assertIn(expected, detail)
        for secret in (self.backend["api_key"], self.bot.settings.GITHUB_TOKEN, self.bot.settings.TELEGRAM_BOT_TOKEN, "unconfigured-secret", "synthetic ledger echo", "\n"):
            self.assertNotIn(secret, detail)

    def test_http_diagnostics_bound_text_and_omit_non_json_bodies(self):
        detail = self.bot._analysis_error_detail(http_error(400, json.dumps({"error": {"message": "x" * 10000}})))
        self.assertLessEqual(len(detail), 612)
        self.assertIn("truncated", detail)
        self.assertEqual(self.bot._analysis_error_detail(http_error(502, "<html>private upstream body</html>")), "HTTPError HTTP 502")

    def test_request_failure_logs_http_status_and_message(self):
        self.bot._supports_tools = MagicMock(return_value=False)
        self.bot._request_llm_message = MagicMock(side_effect=http_error(429, json.dumps({"error": {"message": "quota exceeded"}})))
        with patch("beancounter.analysis.QuerySession", InlineSession), patch("beancounter.analysis.log") as logged:
            self.bot.analyze_ledger("分析开支", "2026-09-10")
        self.assertIn("HTTP 429 message=quota exceeded", str(logged.call_args_list))


class TestHttpSmoke(AnalysisFixture):
    def test_native_tools_through_real_http_and_spawn_worker(self):
        with fake_api([tool_message("capability_probe", {}), {"content": "OK"}, tool_message(), FINAL]) as (url, received):
            self.backend["base_url"] = url
            result = self.bot.analyze_ledger("分析本月开支", "2026-09-10")
        self.assertEqual(len(received), 4)
        self.assertIn("退款", result[0])
        self.assertEqual(received[0]["tools"][0]["function"]["name"], "capability_probe")
        self.assertNotIn("Assets", json.dumps(received[0]))
        self.assertTrue(any(m["role"] == "tool" for m in received[3]["messages"]))
        self.bot.load_ledger.assert_called_once()

    def test_unsupported_api_through_real_http_and_spawn_worker(self):
        with fake_api([(400, {"error": {"message": "tools not supported"}}), QUERY_JSON, FINAL]) as (url, received):
            self.backend["base_url"] = url
            result = self.bot.analyze_ledger("分析本月开支", "2026-09-10")
        self.assertEqual(len(received), 3)
        self.assertNotIn("tools", received[1])
        self.assertIn("退款", result[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
