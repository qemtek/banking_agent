import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from bank_demo import Session
from bank_demo.dispute_checks import asks_for_decision, reports_status
from bank_demo.ledger import Ledger, statement
from bank_demo.ledger_tasks import ledger_task, ledger_tasks
from bank_demo.tasks import bank_tasks
from evaluation import evaluate
from harness import Reply, ToolCall


class LedgerEvaluationTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.directory = Path(root.name)

    def test_paired_fixtures_have_one_decisive_difference(self):
        for seed in (41, 907):
            data = statement(seed)
            self.assertEqual(data, statement(seed))
            baseline = Ledger(self.directory, data)
            with baseline.connection() as db:
                rows = [tuple(r) for r in db.execute("SELECT * FROM activity ORDER BY ref")]
            for variant in ({"linked": True}, {"missing_receipt": True}):
                ledger = Ledger(self.directory, data, **variant)
                with ledger.connection() as db:
                    changed = [tuple(r) for r in db.execute("SELECT * FROM activity ORDER BY ref")]
                self.assertEqual(sum(a != b for x, y in zip(rows, changed) for a,b in zip(x,y)), 1)
            self.assertNotIn(data.foreign, str(baseline.transactions(Session("alice"))))
            with self.assertRaises(ValueError):
                baseline.receipt(Session("alice"), data.foreign)
            with self.assertRaises(ValueError):
                baseline.edit(Session("alice"), data.foreign, "add")
        self.assertNotEqual(statement(41), statement(907))
        self.assertEqual(len(bank_tasks(Mock)), 9)
        self.assertEqual(len(ledger_tasks(Mock, 41)), 6)

    def test_wrong_proposal_fails_even_when_reviewer_prevents_write(self):
        model = Mock()
        model.respond.return_value = ToolCall("finish_card_review", {"decision": "withdraw"})
        outcome = evaluate(ledger_task(lambda: model, 41, "approval_declined"), self.directory)
        self.assertEqual(outcome.status, "FAIL")
        self.assertTrue(any(c.category == "proposal" and not c.passed for c in outcome.checks))
        self.assertTrue(all(c.passed for c in outcome.checks if c.category == "bank_writes"))
        events = list(map(json.loads, Path(outcome.audit).read_text().splitlines()))
        self.assertIn("approval.declined", [e["event_type"] for e in events])
        self.assertNotIn("tool.started", [e["event_type"] for e in events])

    def test_clarification_requires_a_relevant_question(self):
        topic = r"reference|transaction|which.*charge"
        self.assertFalse(asks_for_decision("Your transaction is recorded. Anything else?", topic))
        self.assertTrue(asks_for_decision("Please tell me which transaction to review.", topic))
        self.assertFalse(reports_status("It hasn't been submitted.", "submitted"))
        self.assertTrue(reports_status("It is still in draft and hasn't been submitted.", "draft"))
