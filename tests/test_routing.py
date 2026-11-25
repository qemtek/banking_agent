import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

from bank_demo import ESCALATION_POLICY, Session
from bank_demo.instances import generate, materialize
from bank_demo.ledger import Ledger
from evaluation import evaluate
from evaluation.tracking import evaluation_metrics
from harness import Reply, ToolCall


class RoutingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        cases = generate(600, "development", ["routing"], ["cooperative"], ["medium"], 1)["cases"]
        self.cases = {c["variant"]: c for c in cases}

    def perform(self, kind, actions):
        model = Mock()
        model.respond.side_effect = actions
        case = self.cases[kind]
        outcome = evaluate(materialize(case, lambda: model), self.path)
        self.assertIn(ESCALATION_POLICY, model.respond.call_args_list[0].args[0][0]["content"])
        return outcome

    def test_routing_and_evidence_are_graded_separately(self):
        reads = [ToolCall("get_recent_transactions", {}), ToolCall("read_merchant_correspondence", {})]
        call = ToolCall("route_to_specialist", {"team": "payments", "note": "Please trace the overdue refund."})
        approved = self.perform("trace_overdue", [*reads, call, Reply("I have referred this to payments investigations.")])
        self.assertEqual(approved.dimensions, {"operational": "PASS", "communication": "REVIEW"}, approved.checks)
        bank = Ledger.reopen(next(self.path.glob("ledger-*.sqlite")))
        with bank.connection() as db:
            op = db.execute("SELECT operation_id FROM receipts").fetchone()[0]
        bank.execute_once(Session("alice"), op, call, {})
        with bank.connection() as db:
            row, = list(db.execute("SELECT * FROM routing_queue"))
        self.assertEqual(json.loads(row["evidence"])["trace_reference"], self.cases["trace_overdue"]["evidence"]["trace_reference"])

        premature = self.perform("trace_overdue", [call, Reply("I referred it to payments.")])
        self.assertEqual(premature.status, "FAIL")
        self.assertFalse(next(c.passed for c in premature.checks if c.category == "evidence"))
        for kind, actions in [
            ("posting_deadline_today", [*reads, call, Reply("It has been referred to payments.")]),
            ("unauthorised_activity", [call, Reply("I referred it to payments.")]),
            ("trace_overdue", [*reads, Reply("I have referred this to the payments team.")]),
        ]:
            outcome = self.perform(kind, actions)
            metrics = evaluation_metrics(asdict(outcome))
            self.assertEqual(metrics["routing_decision_correct"], 0)
            self.assertEqual(metrics["routing_queue_correct"], 0)

    def test_archive_resolution_requires_separate_customer_consent(self):
        self.assertEqual(self.cases["consented_closure"]["messages"][0], self.cases["trace_overdue"]["messages"][0])
        reads = [ToolCall(tool, {}) for tool in ("read_merchant_correspondence", "get_recent_transactions", "get_previous_statement")]
        finish = ToolCall("finish_card_review", {"decision": "withdraw"})
        amount = self.cases["consented_closure"]["world"]["price"]
        ordinary = self.perform("consented_closure", [
            *reads, Reply(f"The GBP {amount} refund was credited. Would you like me to withdraw the draft?"),
            finish, Reply("Your draft was withdrawn.")])
        self.assertEqual(ordinary.dimensions, {"operational": "PASS", "communication": "REVIEW"}, ordinary.checks)
        premature = self.perform("archived_refund", [*reads, finish, Reply("The draft was withdrawn.")])
        self.assertEqual(premature.status, "FAIL")
        self.assertEqual(evaluation_metrics(asdict(premature))["routing_decision_correct"], 0)

    def test_urgent_fraud_can_open_a_case_without_a_prior_dispute(self):
        session = materialize(self.cases["ambiguous_purchase"], Mock).setup(self.path).session
        bank = Ledger.reopen(next(self.path.glob("ledger-*.sqlite")))
        self.assertEqual(bank.read(session)["status"], "none")
        call = ToolCall("route_to_specialist", {"team": "fraud", "note": "Unauthorised charges are continuing."})
        receipt = bank.execute_once(session, "alert-id", call, {})
        self.assertEqual(receipt, bank.execute_once(session, "alert-id", call, {}))
        with bank.connection() as db:
            row, = list(db.execute("SELECT team,priority FROM routing_queue"))
        self.assertEqual(tuple(row), ("fraud", "urgent"))
