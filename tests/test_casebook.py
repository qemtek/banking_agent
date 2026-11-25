import json
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from itertools import product
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock, patch

from bank_demo.casebook import actor, run
from bank_demo.card_tasks import CARD_VARIANTS
from bank_demo.instances import BEHAVIOURS, DIFFICULTIES, VARIANTS, digest, generate, materialize, validate
from bank_demo.ledger import gbp
from bank_demo.refund_pairs import freeze
from evaluation import evaluate
from evaluation.uplift import bootstrap_interval, compare
from harness import DEFAULT_MAX_TOOL_CALLS, Reply, ToolCall
from tests.fixtures import BalanceScript


class CasebookTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.directory = Path(root.name)

    def test_matched_refunds_change_only_the_link_and_penalize_an_always_open_agent(self):
        linked, unrelated = freeze(41, 1)["cases"]
        for case, verdict in ((linked, "FAIL"), (unrelated, "PASS")):
            model = Mock()
            model.respond.side_effect = [ToolCall("read_card_review", {}), ToolCall("get_recent_transactions", {}),
                                         Reply("I checked the statement."), Reply("I left the dispute open.")]
            outcome = evaluate(materialize(case, lambda: model), self.directory)
            self.assertEqual(outcome.dimensions["operational"], verdict)
        for case in (linked, unrelated):
            del case["case_id"], case["variant"]
            statement = case["world"]["statement"]
            statement["rows"] = [list(row) for row in statement["rows"]]
            credit = next(row for row in statement["rows"] if row[0] == statement["refund"])
            self.assertEqual(credit[-1], statement["target"] if case is linked else statement["first"])
            credit[-1] = None
        self.assertEqual(linked, unrelated)

    def test_full_grid_and_stable_subsets(self):
        book = generate(41, "development", VARIANTS, BEHAVIOURS, DIFFICULTIES, 1)
        validate(book)
        expected = set(product((v for variants in VARIANTS.values() for v in variants), BEHAVIOURS, DIFFICULTIES))
        self.assertEqual(expected, {(c["variant"], c["behaviour"], c["difficulty"]) for c in book["cases"]})
        for case in book["cases"]:
            task = materialize(case, Mock)
            scenario = task.setup(self.directory)
            self.assertEqual(scenario.agent.max_calls, DEFAULT_MAX_TOOL_CALLS)
            self.assertEqual(len(case["messages"]), 1 + len(task.followups))
            if case["family"] in CARD_VARIANTS:
                self.assertTrue(scenario.state_database.is_file())
            else:
                self.assertIsNotNone(scenario.reviewer)
        subset = generate(41, "development", ["refund"], ["cooperative"], ["medium"], 1)
        ids = {c["case_id"] for c in book["cases"]}
        self.assertTrue(all(c["case_id"] in ids for c in subset["cases"]))
        holdout = generate(41, "holdout", ["refund"], ["cooperative"], ["medium"], 1)
        self.assertFalse(ids.intersection(c["case_id"] for c in holdout["cases"]))
        self.assertEqual(subset, generate(41, "development", ["refund"], ["cooperative"], ["medium"], 1))
        subset["cases"][0]["messages"][0] += "Changed"
        with self.assertRaises(ValueError):
            validate(subset)

    def test_card_session_date_is_shared_with_both_actors(self):
        case, = generate(7, "development", ["card_selection"], ["cooperative"], ["medium"], 1,
                         variants=["select_standard"])["cases"]
        instruction = f"Today's date in this bank session is {case['world']['as_of']}."
        self.assertIn(instruction, materialize(case, Mock).setup(self.directory).agent.instructions)
        self.assertIn(instruction, actor(case, self.directory, Mock).brief)

    def test_parallel_repeats_isolate_state_and_keep_casebook_order_after_a_setup_error(self):
        book = generate(7, "development", ["lookup"], ["cooperative"], ["medium"], 2)
        first, second = book["cases"]
        overlap = Barrier(2, timeout=5)
        def setup(case, factory, instruction):
            overlap.wait()
            if case == first:
                raise ValueError("fixture unavailable")
            return materialize(case, factory, instruction)

        args = Namespace(output=self.directory, arm="parallel", repeats=2, workers=2, backend="local",
                         instructions=None, customer_mode="scripted")
        model = Mock(wraps=BalanceScript(), model_id="fixture", reasoning_effort="fixture")
        with patch("bank_demo.casebook.materialize", setup), patch("bank_demo.casebook.create_model", return_value=model), \
                patch("bank_demo.casebook.publish_if_configured"), redirect_stdout(StringIO()):
            self.assertEqual(run(book, args), 2)
        batch, = self.directory.iterdir()
        rows = json.loads((batch / "report.json").read_text())
        self.assertEqual([(r["case_id"], r["repeat"], r["status"]) for r in rows],
                         [(c["case_id"], n, s) for n in (1, 2) for c, s in ((first, "ERROR"), (second, "REVIEW"))])
        self.assertEqual(len({r["trial_dir"] for r in rows}), 4)
        for row in rows:
            if row["status"] == "REVIEW":
                directory = batch / row["trial_dir"]
                self.assertEqual(len(list(directory.glob("ledger-*.sqlite"))), 1)
                self.assertEqual(Path(row["audit"]).parent, directory)
            else:
                self.assertIn("fixture unavailable", row["error"])

    def test_partial_credit_and_receipt_readback(self):
        book = generate(8, "development", ["refund"], ["misleading"], ["hard"], 1)
        case = next(c for c in book["cases"] if c["variant"] == "refund_partial")
        data = case["world"]["statement"]
        amounts = {row[0]: row[4] for row in data["rows"]}
        remainder = gbp(-amounts[data["target"]] - amounts[data["refund"]])
        actions = iter([
            ToolCall("read_card_review", {}), ToolCall("get_recent_transactions", {}),
            Reply(f"{data['target']}. Outstanding: GBP {remainder}"),
            ToolCall("finish_card_review", {"decision": "submit"}), Reply(f"{data['case']} submitted."),
            ToolCall("read_card_review", {}), None,
        ])
        def respond(messages, tools):
            action = next(actions)
            if action:
                return action
            saved = messages[-1]["value"]
            return Reply(f"{saved['case_id']} {saved['references'][0]} {saved['status']} {saved['receipt_id']}")
        model = Mock()
        model.respond.side_effect = respond
        outcome = evaluate(materialize(case, lambda: model), self.directory)
        self.assertEqual("PASS", outcome.dimensions["operational"])
        self.assertEqual("REVIEW", outcome.status, outcome.error or [c for c in outcome.checks if not c.passed])

    def test_matched_uplift_and_exclusions(self):
        book = generate(12, "development", ["lookup"], ["cooperative"], ["medium"], 4)
        def arm(name, statuses):
            root = self.directory / name
            root.mkdir()
            manifest = dict(suite="generated", arm=name, casebook_id=digest(book), split="development",
                            generator_version=book["version"], score_version=1, model="terra",
                            reasoning_effort="medium", customer_mode="scripted", actor_version=1, repeats=2)
            rows = []
            for case, status_pair in zip(book["cases"], statuses):
                fields = {k: case[k] for k in ("case_id", "world_seed", "family", "variant", "difficulty", "behaviour")}
                for repeat, status in enumerate(status_pair, 1):
                    rows.append({**fields, "repeat": repeat, "status": status, "checks": []})
            for file, obj in (("casebook", book), ("manifest", manifest), ("report", rows)):
                (root / (file + ".json")).write_text(json.dumps(obj))
            return root
        left = arm("baseline", [("FAIL", "FAIL"), ("PASS", "FAIL"), ("PASS", "PASS"), ("ERROR", "PASS")])
        right = arm("candidate", [("PASS", "PASS"), ("PASS", "PASS"), ("FAIL", "PASS"), ("PASS", "PASS")])
        result = compare(left, right)
        self.assertAlmostEqual(100 / 3, result["uplift_pp"])
        self.assertEqual({"win": 3, "loss": 1, "tie": 2}, result["transitions"])
        self.assertEqual(1, result["excluded_cases"])
        self.assertEqual(1, result["execution_errors"]["baseline"])
        self.assertIsNone(result["p_exact"])
        self.assertIsNotNone(result["paired_bootstrap_ci95_pp"])
        self.assertIsNone(bootstrap_interval({"one": [0, 0]}))
        self.assertIsNone(compare(left, right, "initial_amount_correct")["uplift_pp"])
        report = right / "report.json"
        report.write_text(json.dumps(json.loads(report.read_text())[1:]))
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            compare(left, right)
