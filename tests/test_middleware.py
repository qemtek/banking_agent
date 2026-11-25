import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from tests.fixtures import BalanceScript
from bank_demo import Session, build_agent
from harness import Reply, Tool
from harness.middleware import Middleware, ModelChanges


class MiddlewareTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.expiry = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.agent = build_agent(self.directory, model=BalanceScript(), now=lambda: self.expiry)

    def events(self, result):
        return [json.loads(line) for line in (self.directory / (result.run_id + ".jsonl")).read_text().splitlines()]

    def assert_denied(self, agent):
        execute = Mock()
        agent.tools["get_available_balance"] = Tool("Read balance", execute)
        result = agent.run("Balance?", Session("alice", self.expiry))
        self.assertEqual(result.stop_reason, "tool_denied")
        execute.assert_not_called()
        events = self.events(result)
        self.assertNotIn("tool.started", [event["event_type"] for event in events])
        return events

    def test_expired_session_changes_context_and_denies_scripted_call(self):
        events = self.assert_denied(self.agent)
        request = next(e["payload"] for e in events if e["event_type"] == "model.requested")
        self.assertEqual(request["tools"], {})
        self.assertIn("sign in again", request["messages"][0]["content"])
        decisions = [e for e in events if e["event_type"] == "middleware.decided"]
        self.assertEqual(decisions[0]["component"], "SessionAccess")
        self.assertEqual(decisions[0]["payload"]["state_updates"], {"session_expired": True})
        self.assertEqual(decisions[1]["payload"]["denial"]["reason"], "session_expired")
        denied = next(e for e in events if e["event_type"] == "tool.denied")
        self.assertEqual(denied["call_id"], decisions[1]["call_id"])

    def test_session_expires_between_model_and_tool(self):
        clock = Mock(side_effect=[self.expiry - timedelta(seconds=1), self.expiry])
        events = self.assert_denied(build_agent(self.directory, model=BalanceScript(), now=clock))
        request = next(e for e in events if e["event_type"] == "model.requested")
        self.assertIn("get_available_balance", request["payload"]["tools"])
        self.assertEqual(events[-2]["payload"]["reason"], "session_expired")

    def test_state_is_local_and_hooks_see_preceding_changes(self):
        observed = []

        class First(Middleware):
            def before_model(self, session, state, messages, tools):
                observed.append(state.copy())
                state["implicit"] = True
                return ModelChanges(state_updates={"explicit": True}, hide_tools=tuple(tools))

        class Second(Middleware):
            def before_model(self, session, state, messages, tools):
                observed.append((state, tools))
                return ModelChanges()

        self.agent.middleware = [First(), Second()]
        self.agent.model = Mock()
        self.agent.model.respond.return_value = Reply("Done")
        for customer in ["alice", "bob"]:
            self.agent.run("Hello", Session(customer))
        self.assertEqual(observed, [{}, ({"explicit": True}, {}), {}, ({"explicit": True}, {})])

    def test_hidden_tool_cannot_be_dispatched_without_explicit_denial(self):
        class HideTools(Middleware):
            def before_model(self, session, state, messages, tools):
                return ModelChanges(hide_tools=tuple(tools))

        self.agent.middleware = [HideTools()]
        events = self.assert_denied(self.agent)
        self.assertEqual(events[-2]["payload"]["reason"], "tool_hidden")

    def test_hook_failure_stops_dispatch(self):
        class Broken(Middleware):
            def before_tool(self, session, state, call):
                raise ValueError("unavailable")

        self.agent.middleware = [Broken()]
        result = self.agent.run("Balance?", Session("alice"))
        self.assertEqual(result.stop_reason, "middleware_error")
        events = self.events(result)
        self.assertEqual(events[-2]["event_type"], "middleware.failed")
        self.assertNotIn("tool.started", [e["event_type"] for e in events])
