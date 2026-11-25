import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from bank_demo import Session, build_agent
from bank_demo.ledger import Ledger, statement
from bank_demo.ledger_tasks import ReviewWrites
from harness import Reply, ToolCall
from harness.audit import AuditRun
from harness.checkpoints import SQLiteCheckpoints


class DurableReviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.ledger = Ledger(self.directory, statement(41), draft=True)
        self.session = Session("alice", login_id="login")
        self.action = ToolCall("finish_card_review", {"decision": "submit"})
        self.agent = self.new_agent([self.action, Reply("Done")])
        self.chat = self.agent.conversation(self.session)
        self.pending = self.chat.send("Submit")
        self.key = self.pending.proposal.id

    def new_agent(self, actions):
        model = Mock()
        model.respond.side_effect = actions
        bank = Ledger.reopen(self.ledger.path)
        return build_agent(self.directory / "audit", model=model, case_tools=bank.tools(),
                           middleware=(ReviewWrites(bank),),
                           checkpoint_store=SQLiteCheckpoints(self.directory / "checkpoint.sqlite"))

    def test_independent_handles_serialize_review_and_saved_receipt_is_idempotent(self):
        other = self.new_agent([Reply("Done")]).reopen(self.chat.id, self.session)
        def review(handle):
            try:
                return handle.review(self.key, approve=True, reviewer="staff").stop_reason
            except ValueError:
                return "stale"
        with ThreadPoolExecutor(2) as pool:
            self.assertCountEqual(pool.map(review, [self.chat, other]), ["final_reply", "stale"])
        receipt = self.ledger.reconcile(self.session, self.key, self.action)
        self.assertEqual(self.ledger.execute_once(self.session, self.key, self.action, {}), receipt)
        with self.assertRaises(PermissionError):
            self.ledger.execute_once(self.session, self.key, ToolCall("finish_card_review", {"decision": "withdraw"}), {})
        with self.ledger.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM mutations").fetchone()[0], 1)

    def test_missing_bank_file_preserves_an_authorized_operation_until_reconciliation(self):
        with patch.object(Ledger, "execute_once", side_effect=ConnectionError):
            uncertain = self.chat.review(self.key, approve=True, reviewer="staff")
        self.assertEqual(uncertain.stop_reason, "operation_unconfirmed")
        path = self.ledger.path
        parked = path.rename(path.with_suffix(".offline"))
        other = self.new_agent([Reply("Submitted")]).reopen(self.chat.id, self.session)
        self.assertEqual(other.send("Did it go through?").stop_reason, "operation_unconfirmed")
        self.assertFalse(path.exists())
        parked.rename(path)
        self.assertEqual(other.send("Check again").reply, "Submitted")
        self.assertIsNotNone(self.ledger.reconcile(self.session, self.key, self.action))

    def test_decision_and_audit_outbox_survive_a_torn_audit_append(self):
        original = AuditRun.record
        def stop(audit, component, event_type, payload, call_id=None):
            if event_type == "approval.declined":
                with audit.path.open("ab") as stream:
                    stream.write(b'{"run_id":')
                raise InterruptedError("stopped during append")
            return original(audit, component, event_type, payload, call_id)
        with patch.object(AuditRun, "record", stop), self.assertRaises(InterruptedError):
            self.chat.review(self.key, approve=False, reviewer="staff")
        other = self.new_agent([]).reopen(self.chat.id, self.session)
        with self.assertRaises(ValueError):
            other.review(self.key, approve=True, reviewer="staff")
        path = self.directory / "audit" / (self.pending.run_id + ".jsonl")
        events = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([e["event_type"] for e in events[-2:]], ["approval.declined", "run.finished"])
        self.assertIsNone(self.ledger.reconcile(self.session, self.key, self.action))
