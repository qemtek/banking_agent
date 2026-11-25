import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from bank_demo import Session, build_agent
from harness import ContextBudget, Reply
from tests.fixtures import BalanceScript


class ConversationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.agent = build_agent(self.directory, model=BalanceScript())

    def audit(self, result):
        return list(map(json.loads, (self.directory / (result.run_id + ".jsonl")).read_text().splitlines()))

    def first_request(self, events):
        return next(event["payload"] for event in events if event["event_type"] == "model.requested")

    def test_interleaved_sessions_and_new_login_have_separate_histories(self):
        alice = self.agent.conversation(Session("alice"))
        bob = self.agent.conversation(Session("bob"))
        first = alice.send("Alice's first question")
        bob_turn = self.audit(bob.send("Bob's first question"))
        followup = self.audit(alice.send("Can I spend that?"))
        relogin = self.agent.conversation(Session("alice"))
        new_login = self.audit(relogin.send("New login"))
        self.assertEqual(len(self.first_request(bob_turn)["messages"]), 2)
        self.assertEqual(len(self.first_request(new_login)["messages"]), 2)
        messages = self.first_request(followup)["messages"]
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "tool", "assistant", "user"])
        self.assertEqual(messages[3]["value"]["amount"], "1250.75")
        self.assertEqual(messages[4]["content"], first.reply)
        self.assertEqual(followup[0]["payload"]["conversation_id"], alice.id)
        self.assertEqual(followup[0]["payload"]["turn_number"], 2)
        self.assertEqual(len({alice.id, bob.id, relogin.id}), 3)

    def test_expired_session_hides_prior_balance_on_followup(self):
        expiry = datetime.now(timezone.utc)
        now = Mock(return_value=expiry - timedelta(minutes=1))
        agent = build_agent(self.directory, model=BalanceScript(), now=now)
        chat = agent.conversation(Session("alice", expires_at=expiry))
        self.assertEqual(chat.send("Balance?").stop_reason, "final_reply")
        now.return_value = expiry
        followup = self.audit(chat.send("Repeat the last amount"))
        request = self.first_request(followup)
        self.assertEqual(request["tools"], {})
        self.assertNotIn("1250.75", json.dumps(request["messages"]))
        self.assertNotIn("tool.started", [e["event_type"] for e in followup])

    def test_oversized_turn_does_not_poison_next_request(self):
        policy_bytes = len(self.agent.instructions.encode())
        self.agent.context_budget = ContextBudget(window=policy_bytes + 1300, reserve=100)
        self.agent.model = Mock()
        self.agent.model.respond.return_value = Reply("OK")
        chat = self.agent.conversation(Session("alice"))
        self.assertEqual(chat.send("First").stop_reason, "final_reply")
        self.assertEqual(chat.send("Z" * 2000).stop_reason, "context_limit")
        events = self.audit(chat.send("Next"))
        self.assertEqual(events[0]["payload"]["turn_number"], 3)
        self.assertEqual([m["content"] for m in self.first_request(events)["messages"]][1:], ["First", "OK", "Next"])
