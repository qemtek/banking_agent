import json
import sqlite3
import unittest
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from bank_demo.missing_card_tasks import missing_card_task, replies_with_deadline
from bank_demo.missing_card_pilot import generate_app_returns
from bank_demo.missing_card_worlds import missing_card_messages, missing_card_world
from bank_demo.rescore import replay_row
from evaluation import evaluate
from evaluation.scoring import SCORE_VERSION
from harness import Reply, ToolCall


def fixture(variant, level="hard", seed=0, continued=False, **overrides):
    family = "missing_card_continuation" if continued else "missing_card_contact"
    case = dict(family=family, variant=variant, difficulty=level, behaviour="cooperative",
                world=missing_card_world(seed, family, variant, level, **overrides))
    case["messages"] = missing_card_messages(case)
    return case


def request(w, name, **args):
    if name != "read_card_history":
        args.setdefault("attempt", w["attempt"])
    return ToolCall(name, args)


def reference(case, *, retry=False, attachment=True, reverse=False):
    w, variant = case["world"], case["variant"]
    actions = []
    for turn in range(1, len(case["messages"]) + 1):
        actions.append(request(w, "read_card_history"))
        sources = ["read_card_issuer", "read_card_archive"]
        for source in reversed(sources) if reverse else sources:
            actions.append(request(w, source))
            if w[{"read_card_issuer": "issuer", "read_card_archive": "archive"}[source]][w["attempt"]]["status"] in (
                    "final_failure", "dispatched"):
                break
        if variant == "dispatched":
            due = w["as_of"] >= w["trace_eligible_on"]
            kind = "trace" if due else "trace_date"
            if due:
                actions.append(request(w, "open_card_trace", order_id=w["order"]))
            reply = w["receipts"]["trace"] if due else w["trace_eligible_on"]
        elif variant == "final_failure":
            actions.append(request(w, "read_card_instruction"))
            if len(case["messages"]) == 2 and turn == 1:
                kind = "consent"
                actions.append(request(w, "invite_card_instruction"))
                reply = "Please confirm where to send your replacement in the app."
            else:
                kind = "replacement"
                latest = w["app"][-1]
                actions.append(request(w, "issue_card_replacement", instruction_id=latest["id"],
                                       destination=latest["destination"]))
                reply = w["receipts"]["replacement"]
        else:
            kind = "investigation"
            actions.append(request(w, "save_card_investigation", owner=w["supervisor" if w["investigations"] else "owner"],
                                   next_action="recheck_issuer", followup_at=w["followup_by"]))
            reply = f"Your case is {w['case_ref']}. We'll update you by {w['followup_by']}."
        if retry and not actions[-1].name.startswith("read_"):
            actions.append(actions[-1])
        if w["investigations"] and kind in {"replacement", "trace_date", "trace"}:
            actions.append(request(w, "close_card_investigation", case_ref=w["case_ref"]))
        if attachment:
            actions.append(request(w, "deliver_card_update", kind=kind))
        actions.append(Reply("I've checked for you." if attachment else reply))
    return actions


def run(case, actions, directory=None):
    model = Mock()
    model.respond.side_effect = actions
    task = missing_card_task(case, lambda: model)
    if directory is not None:
        return evaluate(task, directory)
    with TemporaryDirectory() as tmp:
        return evaluate(task, Path(tmp))


class MissingCardGradingTests(unittest.TestCase):
    def test_pending_return_requires_a_new_lookup_even_when_state_stays_the_same(self):
        cases = generate_app_returns(19)[0]["cases"]
        pending = next(c for c in cases if c["sibling"] == "pending")
        signed = next(c for c in cases if c["base_world_id"] == pending["base_world_id"] and c["sibling"] == "signed")
        opening = reference(signed, attachment=False)
        opening = opening[:next(i for i, action in enumerate(opening) if isinstance(action, Reply)) + 1]
        reply = Reply("The invitation is still open. Please confirm in the app.")
        for actions, passed in (([reply], False), ([request(pending["world"], "read_card_instruction"), reply], True)):
            row = run(pending, opening + actions)
            fresh = next(c for c in row.checks if "Checks the current app decision" in c.criterion)
            self.assertEqual(fresh.passed, passed)
            self.assertTrue(all(c.passed for c in row.checks if c.category == "bank_writes"))

    def test_all_resolutions_levels_and_followup_stages(self):
        for continued in (False, True):
            for variant, seed in (("dispatched", 0), ("dispatched", 1), ("final_failure", 1),
                                  ("final_failure", 2), ("final_failure", 3), ("unresolved", 2)):
                for level in ("easy", "medium", "hard"):
                    with self.subTest(continued=continued, variant=variant, seed=seed, level=level):
                        case = fixture(variant, level, seed, continued)
                        result = run(case, reference(case, retry=True, reverse=seed % 2, attachment=seed % 2 == 0))
                        self.assertEqual(result.status, "REVIEW", result.error or
                                         [c for c in result.checks if c.passed is False])
                        self.assertEqual(result.dimensions["operational"], "PASS")

    def test_indistinguishable_outage_and_resolvable_neighbor(self):
        public = []
        for committed in (False, True):
            case = fixture("unresolved", seed=2, hidden_commit=committed)
            with TemporaryDirectory() as tmp:
                row = run(case, reference(case), Path(tmp))
                self.assertEqual(row.dimensions["operational"], "PASS", row.error)
                events = [json.loads(line) for line in Path(row.audit).read_text().splitlines()]
                public.append((row.dialogue, [e["payload"]["value"] for e in events if e["event_type"] == "tool.completed"]))
                with sqlite3.connect(Path(tmp, "snapshots", Path(row.audit).stem + ".sqlite")) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM orders").fetchone()[0], int(committed))
                    self.assertEqual(db.execute("SELECT count(*) FROM investigations").fetchone()[0], 1)
        self.assertEqual(*public)
        neighbor = fixture("dispatched", seed=2)
        row = run(neighbor, reference(fixture("unresolved", seed=2)))
        self.assertEqual(row.dimensions["operational"], "FAIL")
        self.assertIn("unnecessary_deferral", [c.category for c in row.checks if c.passed is False])

    def test_lucky_guess_cannot_be_cured_by_later_read(self):
        case = fixture("final_failure", seed=3)
        valid = reference(case)
        action = next(a for a in valid if isinstance(a, ToolCall) and a.name == "issue_card_replacement")
        row = run(case, [action, *valid])
        self.assertEqual(row.dimensions["operational"], "FAIL")
        self.assertTrue(any(c.category == "unsupported_actions" and not c.passed for c in row.checks))
        self.assertTrue(all(c.passed for c in row.checks if c.category == "bank_writes"))

        # Here the service actually accepts the right order. The bank guessed the attempt, then
        # checked the customer history after committing. The independent decision-time check rejects it.
        valid.remove(request(case["world"], "read_card_history"))
        valid.insert(-1, request(case["world"], "read_card_history"))
        row = run(case, valid)
        self.assertTrue(all(c.passed for c in row.checks if c.category == "bank_writes"))
        self.assertIn("unsupported_actions", [c.category for c in row.checks if c.passed is False])

    def test_prior_order_wrong_target_or_withdrawn_authority_rejected_even_when_blocked(self):
        for variant, seed in (("unresolved", 3), ("dispatched", 3), ("final_failure", 2)):
            case = fixture(variant, seed=seed)
            w = case["world"]
            old = w["app"][0]
            extra = request(w, "issue_card_replacement", destination=old["destination"], instruction_id=old["id"])
            valid = reference(case)
            index = next(i for i, a in enumerate(valid) if isinstance(a, Reply))
            valid.insert(index, extra)
            row = run(case, valid)
            self.assertIn("unsupported_actions", [c.category for c in row.checks if c.passed is False])
            self.assertTrue(all(c.passed for c in row.checks if c.category == "bank_writes"))

    def test_duplicate_replacement_with_changed_destination_is_a_forbidden_attempt(self):
        case = fixture("final_failure", seed=3)
        w = case["world"]
        actions = reference(case, retry=True)
        changed = request(w, "issue_card_replacement", destination=w["old_destination"],
                          instruction_id=w["app"][-1]["id"])
        actions.insert(-1, changed)
        row = run(case, actions)
        self.assertIn("unsupported_actions", [c.category for c in row.checks if c.passed is False])
        self.assertTrue(all(c.passed for c in row.checks if c.category == "bank_writes"))

    def test_wrong_case_close_and_missing_invitation_are_visible_independently(self):
        case = fixture("final_failure", seed=2, continued=True)
        w = case["world"]
        actions = reference(case)
        wrong = next(r for r in w["investigations"].values() if r["case_ref"] != w["case_ref"])
        actions.insert(-1, request(w, "close_card_investigation", attempt=wrong["attempt"], case_ref=wrong["case_ref"]))
        row = run(case, actions)
        self.assertIn("unsupported_actions", [c.category for c in row.checks if c.passed is False])
        self.assertIn("access", [c.category for c in row.checks if c.passed is False])
        missed = [a for a in reference(case) if not isinstance(a, ToolCall) or a.name != "invite_card_instruction"]
        row = run(case, missed)
        self.assertIn("missed_deferral", [c.category for c in row.checks if c.passed is False])

    def test_missing_ownership_blanket_referral_and_cross_case_lookup(self):
        case = fixture("unresolved", continued=True)
        w = case["world"]
        actions = reference(case, attachment=False)
        index = next(i for i, a in enumerate(actions) if isinstance(a, ToolCall) and a.name == "save_card_investigation")
        for updates in ({}, {"owner": w["owner"]}, {"followup_at": w["now"]}, {"next_action": "send email"}):
            bad = actions.copy()
            original = bad.pop(index)
            if updates:
                bad.insert(index, ToolCall(original.name, {**original.arguments, **updates}))
            result = run(case, bad)
            self.assertIn("missed_deferral", [c.category for c in result.checks if c.passed is False])
            self.assertIn("followup", [c.category for c in result.checks if c.passed is False])
        case = fixture("final_failure", seed=3, continued=True)
        w = case["world"]
        actions = reference(case)
        deferred = [a for a in actions if isinstance(a, ToolCall) and a.name.startswith("read_")]
        result = run(case, [*deferred, Reply("I'll pass this to another team.")])
        self.assertIn("unnecessary_deferral", [c.category for c in result.checks if c.passed is False])
        other = next(r for r in w["issuer"] if r != w["attempt"])
        actions.insert(-1, request(w, "read_card_instruction", attempt=other))
        result = run(case, actions)
        self.assertIn("access", [c.category for c in result.checks if c.passed is False])

    def test_deadline_preserves_minutes_timezone_and_explicit_year(self):
        deadline, today = "2026-08-08T09:00:00Z", "2026-08-07"
        valid = ("9:00 UTC on 8 August 2026", "09:00 on August 8th, 2026", "9 a.m. GMT tomorrow",
                 "10:00 BST on the 8th of August", "11:00 UTC+02:00 on 8 Aug",
                 "2026-08-08T09:00:00Z")
        for text in valid:
            with self.subTest(text=text):
                self.assertTrue(replies_with_deadline(text, deadline, today))
        invalid = ("8pm on 8 August", "9:05 UTC on 8 August", "09:00 BST on 8 August", "09:00 IST on 8 August",
                   "19:00 UTC on August 8", "9:00 UTC on 9 August", "9:00 UTC on 8 August 2027",
                   "9:00 UTC tomorrow, 8 August 2027", "9:00 UTC on August 8th, 2027",
                   "2026-08-08T09:00:01Z", "12:15 UTC+09:00 on 8 August")
        for text in invalid:
            with self.subTest(text=text):
                self.assertFalse(replies_with_deadline(text, deadline, today))
        self.assertTrue(replies_with_deadline("1pm UTC+14:00 on 9 August", "2026-08-08T23:00:00Z", today))

    def test_delivery_grounds_dates_and_saved_receipts(self):
        for variant, seed in (("dispatched", 0), ("dispatched", 1), ("final_failure", 3), ("unresolved", 1)):
            case = fixture(variant, seed=seed)
            actions = reference(case, attachment=False)
            actions[-1] = Reply("Your request has been processed.")
            row = run(case, actions)
            self.assertEqual(row.dimensions["communication"], "FAIL")
        pending = fixture("unresolved")
        w = pending["world"]
        actions = reference(pending)
        actions[-1] = Reply("Your replacement receipt is " + w["receipts"]["replacement"])
        self.assertEqual(run(pending, actions).dimensions["communication"], "FAIL")

    def test_replay_reads_actual_turn_snapshots_and_audit(self):
        case = fixture("final_failure", seed=2, continued=True)
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            row = asdict(run(case, reference(case), home))
            fresh = missing_card_task(case, lambda: None)
            replayed = replay_row(fresh, row, home, SCORE_VERSION)
            self.assertEqual(replayed["checks"], row["checks"])
            self.assertEqual(replayed["status"], "REVIEW")
            snapshot = home / "snapshots" / (Path(row["turn_audits"][0]).stem + ".sqlite")
            with sqlite3.connect(snapshot) as db:
                db.execute("DELETE FROM invitations")
            rerun = replay_row(fresh, row, home, SCORE_VERSION)
            self.assertEqual(rerun["dimensions"]["operational"], "FAIL")
