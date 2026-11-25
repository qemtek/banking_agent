import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from bank_demo import Session
from bank_demo.followup_tasks import FOLLOWUP_VARIANTS
from bank_demo.instances import generate, materialize
from harness import ToolCall


class FollowupTests(unittest.TestCase):
    def test_evidence_visibility_consent_and_distractors(self):
        cases = generate(718, "development", ["followup"], ["cooperative"], ["hard"], 1)["cases"]
        with tempfile.TemporaryDirectory() as tmp:
            for case in cases:
                scenario = materialize(case, Mock).setup(Path(tmp))
                tools = scenario.agent.tools
                session = scenario.session
                rows = tools["list_card_authorisations"].execute(session)["authorisations"]
                foreign = tools["list_card_authorisations"].execute(Session("bob"))["authorisations"][0]["ref"]
                self.assertNotIn(foreign, str(rows))
                read = tools["read_card_authorisation"].execute
                self.assertEqual(case["world"]["price"], read(session, rows[0]["ref"])["amount"])
                with self.assertRaises(ValueError):
                    read(session, foreign)
                statement = case["world"]["statement"]
                policy, linked = FOLLOWUP_VARIANTS[case["variant"]]
                withdraw = SimpleNamespace(action=ToolCall("finish_card_review", {"decision": "withdraw"}))
                submit = SimpleNamespace(action=ToolCall("finish_card_review", {"decision": "submit"}))
                self.assertEqual(len(case["messages"]), 5)
                for turn in range(1, 6):
                    scenario.before_turn(turn)
                    transactions = tools["get_recent_transactions"].execute(session)["transactions"]
                    credit = [r for r in transactions if r["reference"] == statement["refund"]]
                    self.assertEqual(bool(credit), turn >= case["credit_posts_on_turn"])
                    if credit:
                        self.assertEqual(credit[0]["refund_for"], statement["target" if linked else "first"])
                    self.assertFalse(scenario.reviewer(turn, submit))
                    self.assertEqual(scenario.reviewer(turn, withdraw), turn == 5 and linked and policy == "conditional")
                self.assertFalse(any(word in case["messages"][-1].lower() for word in ("withdraw", "submit", "keep")))
