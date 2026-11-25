import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from adapters.codex_cli import CodexCLI, _require_completed
from adapters.openai_responses import OpenAIResponses
from harness import Harness, Reply, Tool, ToolCall
from harness.audit import LocalAudit
from harness.usage import capture_usage, record_usage
from evaluation.usage import actor_usage, estimate_cost, price_card, summarize
from harness.context import ContextLimitExceeded


class DispatchBudgetTests(unittest.TestCase):
    def test_plus_blocks_oversized_serialized_request_before_launch(self):
        with patch("adapters.codex_cli.subprocess.run") as launch:
            with self.assertRaises(ContextLimitExceeded):
                CodexCLI().respond([{"role": "user", "content": "🚀" * (CodexCLI.context_budget.window // 4)}], {})
            launch.assert_not_called()

    def test_responses_counts_full_payload_and_caps_generation(self):
        adapter = OpenAIResponses("fixture")
        messages = [{"role": "system", "content": "policy"}, {"role": "user", "content": "question"}]
        tools = {"lookup": "Read a record"}
        answer = {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": "Done"}]}]}
        input_limit = adapter.context_budget.window - adapter.context_budget.reserve
        for count in (input_limit + 1, None, True, -1):
            with self.subTest(count=count):
                adapter._post = Mock(return_value={"input_tokens": count})
                with self.assertRaises((ContextLimitExceeded, ValueError)):
                    adapter.respond(messages, tools)
                adapter._post.assert_called_once()
        def post(path, body):
            if path.endswith("input_tokens"):
                self.assertEqual(body, adapter._request(messages, tools))
                return {"input_tokens": input_limit}
            self.assertEqual(body["max_output_tokens"], adapter.context_budget.reserve)
            return {**answer, "usage": {"input_tokens": 80, "output_tokens": 7,
                                       "input_tokens_details": {"cached_tokens": 65},
                                       "output_tokens_details": {"reasoning_tokens": 3}}}
        adapter._post = post
        with capture_usage() as metadata:
            self.assertEqual(adapter.respond(messages, tools), Reply("Done"))
        self.assertEqual(metadata["usage"], {"input_tokens": 80, "output_tokens": 7, "total_tokens": 87,
                                             "cached_input_tokens": 65, "reasoning_output_tokens": 3})

    def test_codex_failed_turn_never_dispatched_as_a_reply(self):
        def result(event):
            return subprocess.CompletedProcess([], 0, json.dumps(event))
        with self.assertRaises(ContextLimitExceeded):
            _require_completed(result({"type": "turn.failed", "error": {"code": "context_length_exceeded"}}))
        with self.assertRaises(ValueError):
            _require_completed(result({"type": "turn.failed", "error": {"message": "service unavailable"}}))
        _require_completed(result({"type": "turn.completed", "usage": {}}))

    def test_provider_counts_survive_a_failed_decision_and_a_missing_usage_report(self):
        with TemporaryDirectory() as directory:
            model = Mock()
            def respond(messages, tools):
                if model.respond.call_count == 1:
                    _require_completed(subprocess.CompletedProcess([], 0, json.dumps({
                        "type": "turn.failed", "usage": {"input_tokens": 70, "output_tokens": 8,
                                                         "cached_input_tokens": 50}})))
                return Reply("Done")
            model.respond.side_effect = respond
            agent = Harness(model, "Help.", {}, LocalAudit(Path(directory)))
            chat = agent.conversation(None)
            results = [chat.send("Can you help?"), chat.send("Please try again.")]
            counts = actor_usage([Path(directory) / (r.run_id + ".jsonl") for r in results])
            self.assertEqual(counts, {"calls": 2, "reported_calls": 1, "reported_tokens": 78})
            with capture_usage() as metadata:
                record_usage({"input_tokens": True, "output_tokens": 7})
            self.assertEqual(metadata, {})

    def test_usage_ratio_includes_unsuccessful_attempts_and_requires_coverage(self):
        def row(status, tokens):
            return {"status": status, "token_usage": {"bank": {"total_tokens": tokens}}}
        passed, failed = row("PASS", 25), row("FAIL", 15)
        self.assertEqual(summarize([passed, failed])["bank"]["tokens_per_verified_resolution"], 40)
        for other in (row("REVIEW", 15), {"status": "ERROR"}):
            self.assertNotIn("tokens_per_verified_resolution", summarize([passed, other])["bank"])

    def test_usd_uses_cached_rate_and_saved_estimate_survives_a_price_change(self):
        card = price_card("codex-standard-2026-11-25")
        manifest = {"model": "gpt-6.1-terra", "price_card": card}
        counts = {"calls": 1, "reported_calls": 1, "input_tokens": 100, "cached_input_tokens": 80,
                  "output_tokens": 10, "reasoning_output_tokens": 3, "total_tokens": 110}
        row = {"status": "PASS", "token_usage": {"bank": counts, "customer": {
            "calls": 0, "reported_calls": 0, "total_tokens": 0}}}
        row["estimated_cost_usd"] = estimate_cost(row, manifest)
        self.assertEqual(row["estimated_cost_usd"]["total"], 0.000074)
        card["usd_per_million_tokens"].clear()
        self.assertEqual(summarize([row], manifest)["estimated_cost_usd"]["total"], 0.000074)
        self.assertNotIn("total", estimate_cost(row, manifest))
        del counts["cached_input_tokens"]
        manifest["price_card"] = price_card("codex-standard-2026-11-25")
        self.assertNotIn("bank", estimate_cost(row, manifest))
        self.assertIsNone(summarize([row, {"status": "ERROR"}], manifest)["estimated_cost_usd"]["total"])

    def test_provider_retry_shrinks_the_decision_without_repeating_tool(self):
        with TemporaryDirectory() as directory:
            model, submit = Mock(), Mock(return_value={"receipt": "r" * 1800})
            agent = Harness(model, "Help.", {"submit": Tool("Submit", submit)}, LocalAudit(Path(directory)))
            model.respond.side_effect = [ToolCall("submit", {}), ContextLimitExceeded(), Reply("Submitted")]
            result = agent.run("Submit", None)
            self.assertEqual(result.reply, "Submitted")
            submit.assert_called_once()
            self.assertIn("read_tool_result", model.respond.call_args.args[1])
            rows = [json.loads(line) for line in (Path(directory) / (result.run_id + ".jsonl")).read_text().splitlines()]
            self.assertEqual(sum(e["event_type"] == "context.retry" for e in rows), 1)
            sizes = [e["payload"]["final_size"] for e in rows if e["event_type"] == "context.checked"]
            self.assertLess(sizes[-1], sizes[-2])
