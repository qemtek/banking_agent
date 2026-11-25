import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

from harness import Harness, Reply, Tool, ToolCall
from harness.audit import LocalAudit
from harness.middleware import Denial, Middleware, ToolDecision


class ReviewPolicy(Middleware):
    enabled = True

    def before_tool(self, session, state, call):
        if not self.enabled:
            return ToolDecision(denial=Denial("expired", "Access expired"))
        return ToolDecision(requires_approval=True)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.write = Mock(return_value={"receipt": "R-1"})
        self.action = ToolCall("change", {"item": "original"})
        self.model = Mock()
        self.model.respond.side_effect = [self.action, Reply("Done")]
        self.policy = ReviewPolicy()
        self.agent = Harness(self.model, "policy", {"change": Tool("Change", self.write, parameters=("item",))},
                             LocalAudit(self.directory), middleware=[self.policy])
        self.chat = self.agent.conversation("alice")

    def events(self, run):
        return list(map(json.loads, (self.directory / (run.run_id + ".jsonl")).read_text().splitlines()))

    def test_concurrent_decisions_execute_only_the_frozen_proposal(self):
        run = self.chat.send("Change original")
        self.write.assert_not_called()
        self.assertEqual(self.events(run)[-1]["event_type"], "approval.requested")
        run.proposal.action.arguments["item"] = "tampered public view"
        self.action.arguments["item"] = "tampered model output"
        with self.assertRaises(ValueError):
            self.agent.conversation("alice").review(run.proposal.id, approve=True, reviewer="B")
        def review(index):
            try:
                return self.chat.review(run.proposal.id, approve=True, reviewer=str(index)).stop_reason
            except ValueError:
                return "stale"
        with ThreadPoolExecutor(2) as pool:
            self.assertCountEqual(pool.map(review, [1, 2]), ["final_reply", "stale"])
        self.write.assert_called_once_with("alice", item="original")
        events = self.events(run)
        linked = [e["event_type"] for e in events if e.get("call_id") == run.proposal.id]
        self.assertLess(linked.index("approval.approved"), linked.index("tool.started"))
        self.assertEqual(linked.count("tool.completed"), 1)

    def test_denial_correction_and_expiry(self):
        for case in ["denial", "correction", "expiry"]:
            with self.subTest(case=case):
                self.policy.enabled = True
                self.model.respond.side_effect = [self.action, Reply("Kept unchanged")]
                chat = self.agent.conversation("alice")
                run = chat.send("Change")
                if case == "correction":
                    self.assertEqual(chat.send("Cancel that").reply, "Kept unchanged")
                else:
                    self.policy.enabled = case != "expiry"
                    result = chat.review(run.proposal.id, approve=case == "expiry", reviewer="staff")
                    self.assertEqual(result.stop_reason, "tool_denied" if case == "expiry" else "approval_declined")
                with self.assertRaises(ValueError):
                    chat.review(run.proposal.id, approve=True, reviewer="staff")
                self.write.assert_not_called()
                self.assertNotIn("tool.started", [e["event_type"] for e in self.events(run)])

    def test_changed_subject_and_replacement_both_require_a_fresh_approval(self):
        class CasePolicy(Middleware):
            amount = 64

            def before_tool(self, session, state, call):
                return ToolDecision(requires_approval=True, review_details={"amount": self.amount})

        policy = CasePolicy()
        self.agent.middleware = [policy]
        original = self.chat.send("Submit the case")
        policy.amount = 18
        stale = self.chat.review(original.proposal.id, approve=True, reviewer="staff")
        self.assertEqual(stale.stop_reason, "approval_superseded")
        self.write.assert_not_called()

        self.model.respond.side_effect = [self.action, ToolCall("change", {"item": "replacement"}), Reply("Done")]
        first = self.chat.send("Submit now")
        updated = self.chat.send("Correction: change the replacement instead")
        with self.assertRaises(ValueError):
            self.chat.review(first.proposal.id, approve=True, reviewer="staff")
        self.write.assert_not_called()
        self.chat.review(updated.proposal.id, approve=True, reviewer="staff")
        self.write.assert_called_once_with("alice", item="replacement")
