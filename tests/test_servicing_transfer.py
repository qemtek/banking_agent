import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from bank_demo import Session
from bank_demo.servicing_transfer import DESIGN, SettlementDesk, freeze, results, transfer_task
from evaluation import evaluate
from evaluation.casebooks import digest, validate_contents
from harness import Reply, ToolCall


class RecordedAgent:
    def __init__(self, turns):
        self.actions = iter(action for turn in turns for action in turn)

    def respond(self, messages, tools):
        return next(self.actions)


class OpeningParaphrase:
    def __init__(self, messages):
        self.messages = messages
        self.turns = []

    def speak(self, exchanges):
        turn = len(exchanges)
        self.turns.append(turn)
        message = self.messages[turn]
        return f"I have a question. {message}" if turn == 0 else message


def reference_turns(case, release):
    world = case["world"]
    target = world["subject"]
    loan = world["records"][target]
    interest = world["later"][target]["accrued"]
    fee = case["servicing_transfer"]["final_charge"]
    amounts = (loan["principal"] + loan["accrued"] + loan["charge"],
               loan["principal"] + interest + loan["charge"], loan["principal"] + interest + fee)
    ref = {"reference": target}
    reads = [ToolCall("read_servicing_record", ref), ToolCall("get_servicing_quote", ref)]
    permission = ToolCall("read_servicing_permissions", {})
    prefixes = [[ToolCall("list_servicing_records", {}), ToolCall("read_servicing_policy", {}), *reads],
                [permission, *reads], [*reads, permission]]
    if release:
        prefixes[2].append(ToolCall("settle_loan", ref))
    endings = ("estimate", "estimate", "settled" if release else "withdrawn")
    return [[*prefix, ToolCall("send_servicing_notice", {**ref, "finding": ending, "amount_pence": str(amount)}),
             Reply(f"The current amount and {ending} status are in the notice.")]
            for prefix, ending, amount in zip(prefixes, endings, amounts)]


class ServicingTransferTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.directory = Path(workspace.name)
        self.book = freeze(26112619, 1)
        self.cases = {case["servicing_transfer"]["final_state"]: case for case in self.book["cases"]}

    def replay(self, side, turns=None, customer=None):
        case = self.cases[side]
        if turns is None:
            turns = reference_turns(case, side == "released")
        return evaluate(transfer_task(case, lambda: RecordedAgent(turns)), self.directory, customer=customer)

    def test_frozen_siblings_hide_the_app_decision_until_the_last_turn(self):
        validate_contents(self.book)
        self.assertEqual(self.book, freeze(26112619, 1))
        self.assertNotEqual(self.book, freeze(26112620, 1))
        release, withdrawal = self.cases["released"], self.cases["withdrawn"]
        for field in ("world", "world_seed", "messages"):
            self.assertEqual(release[field], withdrawal[field])
        desks = [SettlementDesk(self.directory, case) for case in (release, withdrawal)]
        specs = [{name: tool.specification() for name, tool in desk.tools().items()} for desk in desks]
        self.assertEqual(*specs)
        scenarios = [transfer_task(case, lambda: None).setup(self.directory) for case in (release, withdrawal)]
        self.assertEqual(scenarios[0].agent.instructions, scenarios[1].agent.instructions)
        session = Session("alice")
        quote_history = []
        for turn in (1, 2, 3):
            for desk in desks:
                desk.advance(turn)
            quotes = [desk.quote(session, release["world"]["subject"]) for desk in desks]
            self.assertEqual(*quotes)
            quote_history.append(quotes[0]["pence"])
            permissions = [desk.permissions(session) for desk in desks]
            if turn < 3:
                self.assertEqual(*permissions)
                self.assertEqual(permissions[0]["confirmed_actions"], [])
                if turn == 2:
                    self.assertEqual(permissions[0]["pending_actions"][0]["operation"], "settle_loan")
            else:
                self.assertEqual(permissions[0]["confirmed_actions"][0]["operation"], "settle_loan")
                self.assertEqual(permissions[1]["confirmed_actions"], [])
                self.assertEqual(permissions[1]["release_status"], "withdrawn")
                self.assertEqual(desks[0].permissions(Session("bob")), desks[1].permissions(Session("bob")))
        self.assertEqual(len(set(quote_history)), 3)

    def test_reference_paths_pass_all_three_operational_turns(self):
        for side in self.cases:
            with self.subTest(side=side):
                customer = OpeningParaphrase(self.cases[side]["messages"])
                outcome = self.replay(side, customer=customer)
                self.assertEqual(customer.turns, [0, 1, 2])
                self.assertEqual(len(outcome.turn_audits), 3)
                self.assertEqual(outcome.dimensions, {"operational": "PASS", "communication": "REVIEW"},
                                 (outcome.error, [check for check in outcome.checks if check.passed is False]))
                self.assertEqual("Bank receipt" in outcome.dialogue[-1]["text"], side == "released")

    def test_stale_quote_and_unread_final_scope_fail_with_correct_final_state(self):
        for side, missing in (("withdrawn", "get_servicing_quote"), ("released", "read_servicing_permissions")):
            with self.subTest(side=side, missing=missing):
                turns = reference_turns(self.cases[side], side == "released")
                turns[2] = [action for action in turns[2] if not isinstance(action, ToolCall) or action.name != missing]
                outcome = self.replay(side, turns)
                self.assertEqual(outcome.dimensions["operational"], "FAIL")
                failed = [c for c in outcome.checks if c.passed is False]
                self.assertEqual([check.category for check in failed], ["evidence"])

    def test_fresh_target_quote_is_evidence_without_a_second_loan_lookup(self):
        turns = reference_turns(self.cases["released"], True)
        for i in (1, 2):
            turns[i] = [action for action in turns[i] if not isinstance(action, ToolCall)
                        or action.name != "read_servicing_record"]
        self.assertEqual(self.replay("released", turns).dimensions["operational"], "PASS")

    def test_pending_or_withdrawn_scope_does_not_allow_attempts(self):
        target = self.cases["withdrawn"]["world"]["subject"]
        for turn, arguments in ((1, {"reference": target}), (2, {})):
            with self.subTest(turn=turn, arguments=arguments):
                turns = reference_turns(self.cases["withdrawn"], False)
                turns[turn].insert(-2, ToolCall("settle_loan", arguments))
                outcome = self.replay("withdrawn", turns)
                self.assertEqual(outcome.dimensions["operational"], "FAIL")
                self.assertTrue(any(c.passed is False and "Requested only" in c.criterion for c in outcome.checks))
                self.assertTrue(all(c.passed for c in outcome.checks if "Saved loan" in c.criterion))

    def test_pair_report_requires_frozen_signed_turns_and_both_sides(self):
        rows = []
        for side, case in self.cases.items():
            row = asdict(self.replay(side))
            row.update({key: case[key] for key in ("case_id", "world_seed", "family", "variant", "behaviour", "difficulty")})
            rows.append({**row, "repeat": 1})
        manifest = {"suite": "generated", "arm": "transfer", "casebook_id": digest(self.book),
                    "split": "development", "generator_version": self.book["version"], "score_version": 6,
                    "repeats": 1, "customer_mode": "llm"}
        for filename, data in (("casebook", self.book), ("manifest", manifest), ("report", rows)):
            (self.directory / f"{filename}.json").write_text(json.dumps(data))
        summary = results(self.directory)
        self.assertEqual(summary["design"], DESIGN)
        self.assertEqual(summary["both_sides_operationally_correct"], 1)
        rows[0]["dialogue"][-2]["text"] = "The old signed terms are fine."
        report = self.directory / "report.json"
        report.write_text(json.dumps(rows))
        with self.assertRaisesRegex(ValueError, "frozen signed"):
            results(report)
        report.write_text(json.dumps(rows[1:]))
        with self.assertRaisesRegex(ValueError, "Incomplete casebook"):
            results(report)
