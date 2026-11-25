import json
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from tests.fixtures import BalanceScript
from bank_demo import Session, balance_facts, build_agent
from harness import ContextBudget, Harness, Reply, Tool, ToolCall
from harness.audit import LocalAudit
from harness.context import request_bytes
from harness.middleware import Middleware, ModelChanges


class ContextTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.model = Mock()
        self.tool = Mock()
        self.agent = Harness(self.model, "policy", {"lookup": Tool("Read record", self.tool)},
                             LocalAudit(self.directory))
        self.descriptions = {"lookup": "Read record"}

    def events(self, result, kind):
        path = self.directory / (result.run_id + ".jsonl")
        events = map(json.loads, path.read_text().splitlines())
        return [event["payload"] for event in events if event["event_type"] == kind]

    def test_exact_limit_and_one_byte_over_after_middleware(self):
        class AddInstruction(Middleware):
            def before_model(self, session, state, messages, tools):
                return ModelChanges(messages=[{"role": "system", "content": "Résumé"}, *messages])

        self.agent.middleware = [AddInstruction()]
        self.model.respond.return_value = Reply("OK")
        question = "£?"
        messages = [{"role": "system", "content": "Résumé"}, {"role": "system", "content": "policy"},
                    {"role": "user", "content": question}]
        size = request_bytes(messages, self.descriptions)
        self.agent.context_budget = ContextBudget(window=size + 20, reserve=20)
        self.assertEqual(self.agent.run(question, None).stop_reason, "final_reply")
        result = self.agent.run(question + "!", None)
        self.assertEqual(result.stop_reason, "context_limit")
        self.assertEqual(self.events(result, "context.checked")[0]["final_size"], size + 1)
        self.model.respond.assert_called_once_with(messages, self.descriptions)
        self.assertEqual(self.events(result, "model.requested"), [])

    def test_balance_comparison_uses_both_observations_after_compaction(self):
        agent = build_agent(self.directory, model=BalanceScript())
        policy_bytes = len(agent.instructions.encode())
        agent.context_budget = ContextBudget(window=policy_bytes + 1530, reserve=100)
        lookup = Mock(side_effect=[
            {"amount": "1250.75", "currency": "GBP", "detail": "a" * 530},
            {"amount": "1200.25", "currency": "GBP", "detail": "b" * 530},
        ])
        summarize = Mock(wraps=balance_facts)
        name = "get_available_balance"
        agent.tools = {name: Tool("Read balance", lookup, summarize)}

        def compare(messages, tools):
            if len(messages) < 5:
                return ToolCall(name, {})
            first, second = messages[-3]["facts"], messages[-1]["value"]
            change = Decimal(first["amount"]) - Decimal(second["amount"])
            return Reply(f"Your available balance fell by {first['currency']} {change}.")

        agent.model = Mock()
        agent.model.respond.side_effect = compare
        result = agent.run("Compare my two balance lookups.", Session("alice"))
        self.assertEqual(result.reply, "Your available balance fell by GBP 50.50.")
        requests = self.events(result, "model.requested")
        last = requests[-1]["messages"]
        self.assertEqual(last[:2], requests[0]["messages"])
        summary = last[2]
        self.assertEqual(summary["facts"], {"amount": "1250.75", "currency": "GBP"})
        self.assertEqual(summary["call_id"], requests[1]["messages"][-1]["call_id"])
        self.assertEqual(last[3]["call_id"], last[4]["call_id"])
        self.assertNotEqual(summary["call_id"], last[4]["call_id"])
        limit = agent.context_budget.window - agent.context_budget.reserve
        self.assertTrue(all(request_bytes(r["messages"], r["tools"]) <= limit for r in requests))
        self.assertEqual(self.events(result, "context.compacted")[0]["summaries"], [summary])
        self.assertEqual(len(self.events(result, "tool.completed")), 2)

    def test_summary_reused_only_while_source_still_visible(self):
        from harness.context import RunContext

        summarize = Mock(side_effect=lambda value: {"amount": value["amount"]})
        context = RunContext(ContextBudget(window=720, reserve=20), {"lookup": summarize})
        audit = Mock()
        def pair(call_id, amount):
            return [{"role": "assistant", "call_id": call_id,
                     "tool_call": {"name": "lookup", "arguments": {"record": call_id}}},
                    {"role": "tool", "call_id": call_id, "name": "lookup",
                     "value": {"amount": amount, "detail": "x" * 210}}]

        messages = pair("first", "10") + pair("second", "20")
        initial = context.prepare(messages, {}, audit)
        self.assertEqual(initial[0]["facts"], {"amount": "10"})
        self.assertEqual(initial[0]["arguments"], {"record": "first"})
        self.assertEqual(context.prepare(messages, {}, audit), initial)
        summarize.assert_called_once()
        self.assertEqual(context.prepare(messages[2:], {}, audit), messages[2:])
        messages[1]["value"]["amount"] = "redacted"
        updated = context.prepare(messages, {}, audit)
        self.assertEqual(updated[0]["facts"], {"amount": "redacted"})

    def test_overflow_without_summary_policy_preserves_facts_and_stops(self):
        class HideReader(Middleware):
            def before_model(self, session, state, messages, tools):
                return ModelChanges(hide_tools=("read_tool_result",))

        self.agent.middleware = [HideReader()]
        self.agent.context_budget = ContextBudget(window=1200, reserve=100)
        self.model.respond.return_value = ToolCall("lookup", {})
        self.tool.return_value = {"detail": "x" * 640}
        result = self.agent.run("Compare both", None)
        self.assertEqual(result.stop_reason, "context_limit")
        self.assertEqual(self.model.respond.call_count, 2)

    def test_large_latest_tool_result_stops_next_call(self):
        self.agent.context_budget = ContextBudget(window=500, reserve=50)
        self.model.respond.return_value = ToolCall("lookup", {})
        self.tool.return_value = {"data": "x" * 1000}
        result = self.agent.run("Lookup", None)
        self.assertEqual(result.stop_reason, "context_limit")
        self.model.respond.assert_called_once()
        self.tool.assert_called_once()
        self.assertFalse(self.events(result, "context.checked")[-1]["fits"])

    def test_measurement_failure_prevents_dispatch(self):
        for counter in [Mock(side_effect=ValueError()), Mock(return_value=-1)]:
            self.agent.context_budget = ContextBudget(measure=counter)
            result = self.agent.run("Lookup", None)
            self.assertEqual(result.stop_reason, "context_error")
            self.assertEqual(len(self.events(result, "context.failed")), 1)
        self.model.respond.assert_not_called()
