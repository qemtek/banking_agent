import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from bank_demo.instances import BEHAVIOURS, DIFFICULTIES, generate, generate_independent, materialize, validate
from bank_demo.provisional_credit import OUTCOMES
from bank_demo.rescore import replay_row
from evaluation import evaluate
from evaluation.casebooks import digest
from evaluation.scoring import SCORE_VERSION
from harness import Reply, ToolCall


def reference(case):
    w = case["world"]
    dispute = {"dispute_ref": w["case_ref"]}
    file = ToolCall("read_dispute_file", {})
    ledger = ToolCall("read_credit_postings", {})
    reopen = ToolCall("reopen_with_relief", dispute)
    send = ToolCall("send_dispute_statement", dispute)
    actions = [ledger, file, reopen, reopen, send, Reply("Please see your statement."), ledger]
    new = w["incoming"]
    if new["status"] == "posted" and new["purchase"] == w["purchase"]:
        offset = ToolCall("offset_bank_credit", {**dispute, "merchant_ref": new["reference"], "pence": str(new["pence"])})
        actions.extend([offset, offset])
    return [*actions, send, Reply("Your statement is in the app.")]


def run(case, actions):
    model = Mock()
    model.respond.side_effect = actions
    task = materialize(case, lambda: model)
    with TemporaryDirectory() as tmp:
        directory = Path(tmp)
        result = evaluate(task, directory)
        assert not result.error, result.error
        replay = replay_row(task, asdict(result), directory, SCORE_VERSION)
        # Replays locate restored state by snapshot name.
        assert replay["checks"] == asdict(result)["checks"]
    return result


class CreditTests(unittest.TestCase):
    def test_grid_and_source_split(self):
        cases = generate(67, "development", ["refund"], BEHAVIOURS, DIFFICULTIES, 1, OUTCOMES)
        validate(cases)
        self.assertEqual(60, len(cases["cases"]))
        for case in cases["cases"]:
            result = run(case, reference(case))
            self.assertEqual(result.status, "REVIEW")
            self.assertEqual(result.dimensions["operational"], "PASS")
        group = [c for c in cases["cases"] if c["behaviour"] == "uncertain" and c["difficulty"] == "hard"]
        baseline = next(c for c in group if c["variant"] == "merchant_refund_full")
        for c in group:
            w = deepcopy(c["world"])
            new = w.pop("incoming")
            base = dict(baseline["world"])
            original = base.pop("incoming")
            self.assertEqual(w, base)
            self.assertEqual(c["messages"], baseline["messages"])
            self.assertEqual(sum(new[k] != original[k] for k in original), int(c is not baseline))
        for generate_book in (generate, generate_independent):
            self.assertFalse(generate_book(67, "holdout", ["refund"], ["cooperative"], ["hard"], 2, OUTCOMES)["cases"])
        altered = deepcopy(baseline)
        altered["split"] = "holdout"
        altered.pop("case_id")
        altered["case_id"] = digest([cases["version"], altered])
        with self.assertRaises(ValueError):
            validate({**cases, "split": "holdout", "cases": [altered]})

    def test_early_excessive_unrelated_and_missing_reversals(self):
        for c in generate(67, "development", ["refund"], ["cooperative"], ["hard"], 1, OUTCOMES)["cases"]:
            w = c["world"]
            new = w["incoming"]
            bad_offset = ToolCall("offset_bank_credit", {"dispute_ref": w["case_ref"], "merchant_ref": new["reference"],
                                                        "pence": str(w["amount"])})
            for where in (2, 7):
                steps = reference(c)
                if where == 7 and c["variant"] == "merchant_refund_full":
                    # Correct settlement before refreshing the ledger. A later read cannot authorize the earlier write.
                    where = 6
                steps.insert(where, bad_offset)
                self.assertEqual(run(c, steps).dimensions["operational"], "FAIL")
            if c["variant"] in ("merchant_refund_partial", "merchant_refund_full"):
                self.assertEqual(run(c, [a for a in reference(c) if not isinstance(a, ToolCall) or
                                         a.name != "offset_bank_credit"]).dimensions["operational"], "FAIL")
            steps = reference(c)
            steps.insert(2, ToolCall("send_dispute_statement", {"dispute_ref": w["case_ref"]}))
            self.assertEqual(run(c, steps).dimensions["communication"], "FAIL")
