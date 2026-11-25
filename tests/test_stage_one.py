import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from tests.fixtures import BalanceScript
from bank_demo import Session, build_agent
from harness import Harness, Reply, Tool, ToolCall
from harness.audit import LocalAudit


class StageOneTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def events(self, run_id):
        path = self.directory / (run_id + ".jsonl")
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_balance_and_audit_are_scoped_to_each_run(self):
        agent = build_agent(self.directory, model=BalanceScript())
        for customer, amount in [("alice", "1250.75"), ("bob", "84.20")]:
            result = agent.run("What's my available balance?", Session(customer))
            self.assertEqual(result.reply, f"Your available balance is GBP {amount}.")
            events = self.events(result.run_id)
            self.assertEqual(events[0]["payload"]["session"]["customer_id"], customer)
            core_events = [e for e in events if not e["event_type"].startswith("middleware.")]
            self.assertEqual([e["event_type"] for e in core_events], [
                "run.started", "context.checked", "model.requested", "model.responded", "tool.requested",
                "tool.started", "tool.completed", "context.checked", "model.requested", "model.responded", "run.finished",
            ])
            self.assertEqual(len({e["call_id"] for e in events if "call_id" in e}), 1)
            self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(len(list(self.directory.glob("*.jsonl"))), 2)

    def test_model_cannot_supply_alternate_identity(self):
        agent = build_agent(self.directory, model=BalanceScript())
        agent.model = Mock()
        agent.model.respond.return_value = ToolCall("get_available_balance", {"customer_id": "bob"})
        result = agent.run("Look up Bob", Session("alice"))
        self.assertEqual(result.stop_reason, "invalid_tool_call")
        self.assertNotIn("tool.started", [e["event_type"] for e in self.events(result.run_id)])

    def test_failed_audit_stops_dispatch(self):
        def select(event, payload):
            if event == "tool.started":
                raise OSError("storage failure")
            return payload

        execute = Mock()
        model = Mock()
        model.respond.return_value = ToolCall("example", {})
        agent = Harness(model, "", {"example": Tool("", execute)}, LocalAudit(self.directory, select))
        with self.assertRaises(OSError):
            agent.run("example", None)
        execute.assert_not_called()

    def test_lookup_failure_preserves_outcome(self):
        result = build_agent(self.directory, model=BalanceScript()).run("Balance?", Session("unknown"))
        self.assertEqual(result.stop_reason, "tool_error")
        self.assertEqual(self.events(result.run_id)[-2]["event_type"], "tool.failed")

    def test_model_failure_is_recorded_before_run_finishes(self):
        agent = build_agent(self.directory, model=BalanceScript())
        agent.model = Mock()
        agent.model.respond.side_effect = TimeoutError()
        result = agent.run("Balance?", Session("alice"))
        self.assertEqual(result.stop_reason, "model_error")
        events = self.events(result.run_id)
        self.assertEqual(events[-2]["event_type"], "model.failed")
        self.assertEqual(events[-2]["payload"], {"error": "TimeoutError"})

    def test_completion_audit_failure_propagates_after_tool_runs_once(self):
        def select(event, payload):
            if event == "tool.completed":
                raise OSError("storage failure")
            return payload

        agent = build_agent(self.directory, model=BalanceScript())
        execute = Mock(return_value={"amount": "84.20", "currency": "GBP"})
        agent.tools["get_available_balance"] = Tool("", execute)
        agent.audit = LocalAudit(self.directory, select)
        with self.assertRaises(OSError):
            agent.run("Balance?", Session("bob"))
        execute.assert_called_once_with(Session("bob"))

    def test_repeated_model_requests_are_bounded(self):
        agent = build_agent(self.directory, model=BalanceScript())
        agent.model = Mock()
        agent.max_calls = 4
        agent.model.respond.return_value = ToolCall("get_available_balance", {})
        result = agent.run("Balance?", Session("alice"))
        self.assertEqual(result.stop_reason, "call_limit")
        self.assertEqual(sum(e["event_type"] == "tool.started" for e in self.events(result.run_id)), 4)

    def test_default_allows_work_beyond_the_old_family_limits(self):
        model = Mock()
        model.respond.side_effect = [ToolCall("get_recent_transactions", {}) for _ in range(13)] + [Reply("Done")]
        result = build_agent(self.directory, model=model).run("Review the records.", Session("alice"))
        self.assertEqual(result.stop_reason, "final_reply")
        events = self.events(result.run_id)
        self.assertEqual(sum(e["event_type"] == "tool.completed" for e in events), 13)
