import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from bank_demo import Session
from bank_demo.archive_case import freeze_handoff
from bank_demo.evidence import EVIDENCE_VARIANTS, EvidenceDesk
from bank_demo.instances import generate, materialize
from bank_demo.ledger import Ledger
from evaluation import evaluate
from evaluation.review import apply, packet as review_packet
from evaluation.casebooks import digest
from harness import Reply, ToolCall


class EvidenceTests(unittest.TestCase):
    def test_decisions_and_prior_thread_continuation(self):
        book = generate(98, "development", ["evidence", "handoff"], ["cooperative"], ["hard"], 1)
        self.assertEqual(len({tuple(c["messages"]) for c in book["cases"] if c["variant"] in EVIDENCE_VARIANTS}), 1)
        with tempfile.TemporaryDirectory() as root:
            for case in book["cases"]:
                directory = Path(root) / case["variant"]
                directory.mkdir()
                data, packet = case["world"]["statement"], case["evidence"]
                handoff = case["family"] == "handoff"
                kind = case["variant"].removeprefix("handoff_")
                actions = [ToolCall("read_card_review", {})]
                if handoff:
                    actions.extend([ToolCall("list_previous_conversations", {}),
                                    ToolCall("resume_previous_conversation", {"conversation_id": packet["prior_id"]})])
                actions.extend([ToolCall("read_merchant_correspondence", {}), ToolCall("get_recent_transactions", {})])
                if kind == "archived_credit":
                    actions.append(ToolCall("get_previous_statement", {}))
                amount = "0.00" if kind == "archived_credit" else case["world"]["price"]
                findings = f"{data['target']} has GBP {amount} outstanding. " + packet.get("enquiry", "")
                if kind == "conflicting_letter":
                    findings += " The merchant claims it issued a refund but there is no matching posted credit."
                actions.append(Reply(findings))
                status = {"complete_statement": "submitted", "archived_credit": "withdrawn",
                          "conflicting_letter": "referred", "retain_draft": "draft", "prepare_handoff": "draft"}[kind]
                if kind == "conflicting_letter":
                    actions.append(ToolCall("refer_card_review", {}))
                elif status != "draft":
                    actions.append(ToolCall("finish_card_review", {"decision": "submit" if status == "submitted" else "withdraw"}))
                summary = findings + " Wait for the customer's confirmation." if kind == "prepare_handoff" else ""
                actions.append(Reply(f"{data['case']} is {status}. {summary}".strip()))
                model = Mock()
                model.respond.side_effect = actions
                outcome = evaluate(materialize(case, lambda: model), directory)
                self.assertEqual("PASS", outcome.dimensions["operational"])
                self.assertEqual("REVIEW", outcome.status, outcome.error or [c for c in outcome.checks if not c.passed])
                if kind == "retain_draft":
                    single = {**book, "cases": [case]}
                    for filename, contents in (("casebook", single), ("manifest", {"casebook_id": digest(single), "score_version": 5}),
                                               ("report", [{**asdict(outcome), "case_id": case["case_id"], "repeat": 1}])):
                        (directory / (filename + ".json")).write_text(json.dumps(contents))
                    with self.assertRaises(ValueError):
                        freeze_handoff(directory)
                    report = directory / "report.json"
                    labels = review_packet(report)
                    for criterion in labels["items"][0]["criteria"]:
                        criterion["label"] = "pass"
                    reviewed, _ = apply(report, labels, "fixture")
                    reviewed_path = directory / "reviewed.json"
                    reviewed_path.write_text(json.dumps(reviewed))
                    transfer = freeze_handoff(reviewed_path)["cases"][0]
                    self.assertNotEqual(transfer["case_id"], case["case_id"])
                    self.assertEqual(transfer["previous_reply"], outcome.dialogue[-1]["text"])
                bank = Ledger.reopen(next(directory.glob("ledger-*.sqlite")))
                desk = EvidenceDesk(bank)
                if kind == "archived_credit":
                    recent, previous = desk.recent(Session("alice")), desk.previous(Session("alice"))
                    self.assertLess(previous["coverage_through"], recent["coverage_from"])
                    self.assertNotIn(data["refund"], str(recent))
                    self.assertIn(data["refund"], str(previous))
                if handoff:
                    alice = Session("alice")
                    listed = desk.list_threads(alice)
                    foreign = next(t["id"] for t in packet["threads"] if t["owner"] == "bob")
                    self.assertNotIn(foreign, str(listed))
                    for bad in (foreign, "absent"):
                        with self.assertRaisesRegex(ValueError, "Conversation unavailable"):
                            desk.resume(alice, bad)
                    dialogue = desk.resume(alice, packet["prior_id"])["dialogue"]
                    self.assertEqual(dialogue[-1], ["adviser", actions[-1].text])
                    self.assertEqual(len(dialogue), 8)
                    # Another adviser can continue the thread after reopening the bank.
                    result = SimpleNamespace(run_id="next", reply="I've picked up the case.")
                    for _ in range(2):
                        desk.record_exchange(alice, "Can you help again?", result)
                    self.assertEqual(len(desk.resume(alice, packet["prior_id"])["dialogue"]), 10)
                if kind == "conflicting_letter":
                    with bank.connection() as db:
                        op = db.execute("SELECT operation_id FROM receipts").fetchone()[0]
                    original = bank.execute_once(Session("alice"), op, ToolCall("refer_card_review", {}), {})
                    self.assertEqual(original["status"], "referred")
                    with bank.connection() as db:
                        self.assertEqual(db.execute("SELECT count(*) FROM referrals").fetchone()[0], 1)
