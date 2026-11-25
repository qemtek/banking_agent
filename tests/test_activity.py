import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from bank_demo import Session, build_agent
from bank_demo.activity_tasks import setup_activity
from harness import Reply, ToolCall


class ActivityTests(unittest.TestCase):
    def test_account_scope_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Mock()
            call = ToolCall("get_recent_transactions", {})
            model.respond.side_effect = [call, Reply("Done"), call, Reply("Done"), call]
            now = datetime.now(timezone.utc)
            agent = build_agent(Path(directory), model=model, now=lambda: now)
            for customer, reference in [("alice", "TX-4102"), ("bob", "TX-8701")]:
                agent.run("Show my activity", Session(customer))
                messages, _ = model.respond.call_args.args
                rows = messages[-1]["value"]["transactions"]
                self.assertEqual([row["reference"] for row in rows], [reference])
            result = agent.run("Show my activity", Session("alice", expires_at=now))
            self.assertEqual(result.stop_reason, "tool_denied")
            _, tools = model.respond.call_args.args
            self.assertEqual(tools, {})

    def test_reads_preserve_fixture_and_turn_boundary_advances_both_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            for confirmed in [True, False]:
                fixture = setup_activity(Path(directory), model_factory=Mock, confirmed=confirmed)
                balance = fixture.agent.tools["get_available_balance"].execute
                activity = fixture.agent.tools["get_recent_transactions"].execute
                self.assertEqual(balance(fixture.session), balance(fixture.session))
                self.assertEqual(balance(fixture.session)["amount"], "1250.75")
                self.assertEqual(len(activity(fixture.session)["transactions"]), 1)
                fixture.before_turn(2)
                self.assertEqual(balance(fixture.session)["amount"], "1200.25")
                records = activity(fixture.session)
                self.assertEqual(records["coverage_through"], "09:05" if confirmed else "09:01")
                self.assertEqual(len(records["transactions"]), 2 if confirmed else 1)
