import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from bank_demo import Session, build_agent
from bank_demo.activity_tasks import amount_in
from bank_demo.casework import ReviewDesk
from harness import Reply, ToolCall


class CaseworkTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.session = Session("alice")
        self.model = Mock()
        self.desk = ReviewDesk("alice", "CR-49", [{"reference": "TX-7", "merchant": "Bookshop",
                                                   "amount": "-12.50", "receipt": "Map"}])
        self.agent = build_agent(Path(temp.name), model=self.model, case_tools=self.desk.tools())

    def test_rejects_invalid_arguments_and_other_accounts_before_mutation(self):
        for arguments in [{"reference": "TX-7"}, {"reference": 7, "operation": "add"},
                          {"reference": "TX-7", "operation": "add", "customer_id": "bob"}]:
            self.model.respond.return_value = ToolCall("edit_card_review", arguments)
            self.assertEqual(self.agent.run("Open", self.session).stop_reason, "invalid_tool_call")
        self.model.respond.return_value = ToolCall("edit_card_review", {"reference": "TX-7", "operation": "add"})
        self.assertEqual(self.agent.run("Open", Session("bob")).stop_reason, "tool_error")
        self.assertEqual(self.agent.run("Open", Session("alice", datetime.now(timezone.utc))).stop_reason, "tool_denied")
        self.assertEqual(self.desk.revisions, [])

    def test_amount_at_sentence_end(self):
        self.assertTrue(amount_in("The total is GBP 96.00.", "96.00"))
        self.assertFalse(amount_in("GBP 196.00", "96.00"))
        self.assertFalse(amount_in("GBP 96.009", "96.00"))

    def test_terminal_case_cannot_be_reopened_or_submitted_again(self):
        self.model.respond.side_effect = [
            ToolCall("edit_card_review", {"reference": "TX-7", "operation": "add"}), Reply("Draft opened"),
            ToolCall("finish_card_review", {"decision": "submit"}), Reply("Filed"),
        ]
        chat = self.agent.conversation(self.session)
        chat.send("Open")
        chat.send("Submit")
        self.assertEqual(self.desk.snapshot()["status"], "submitted")
        with self.assertRaises(ValueError):
            self.desk.finish(self.session, "submit")
        with self.assertRaises(ValueError):
            self.desk.edit(self.session, "TX-7", "remove")
        self.assertEqual(self.desk.revisions, [("add", "TX-7"), ("submit", "CR-49")])
