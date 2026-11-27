import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from bank_demo import declined_card
from bank_demo.instances import BEHAVIOURS, DIFFICULTIES, generate, generate_independent, materialize, validate
from bank_demo.rescore import replay_row
from evaluation import evaluate
from evaluation.casebooks import digest
from evaluation.scoring import SCORE_VERSION
from harness import Reply, ToolCall


def book(seed=51, split="development", generator=generate):
    return generator(seed, split, [declined_card.FAMILY], BEHAVIOURS, DIFFICULTIES, 1, declined_card.OUTCOMES)


def actions(case):
    w = case["world"]
    ref = w["instructions"][-1]
    calls = [ToolCall("read_declines", {}), ToolCall("read_card_status", {"card_id": w["card"]}),
             ToolCall("read_shipment", {"order_id": w["order"]}),
             ToolCall("read_mailing_instruction", {"order_id": w["order"]})]
    if ref["status"] == "signed":
        change = ToolCall("order_replacement", {"order_id": w["order"], "instruction_id": ref["id"],
                                                "destination": ref["destination"]})
        return [*calls, change, Reply(w["receipt"])]
    return [*calls, ToolCall("request_mailing_confirmation", {"order_id": w["order"]}), Reply(w["invitation"])]


def execute(case, steps):
    model = Mock()
    model.respond.side_effect = steps
    task = materialize(case, lambda: model)
    with TemporaryDirectory() as tmp:
        directory = Path(tmp)
        result = evaluate(task, directory)
        assert not result.error, result.error
        replay = replay_row(task, asdict(result), directory, SCORE_VERSION)
        assert replay["checks"] == asdict(result)["checks"]
    return result


class SourceDerivedCardTests(unittest.TestCase):
    def test_counterfactuals_and_replay(self):
        frozen = book()
        validate(frozen)
        self.assertEqual(30, len(frozen["cases"]))
        groups = {}
        for case in frozen["cases"]:
            result = execute(case, actions(case))
            self.assertEqual("PASS", result.dimensions["operational"])
            groups.setdefault((case["behaviour"], case["difficulty"]), []).append(case)
        for a, b in groups.values():
            self.assertEqual(a["source_group"], b["source_group"])
            self.assertEqual(a["messages"], b["messages"])
            w = deepcopy(a["world"])
            w["instructions"][-1]["status"] = b["world"]["instructions"][-1]["status"]
            self.assertEqual(w, b["world"])

    def test_source_split(self):
        for generator in (generate, generate_independent):
            self.assertFalse(book(split="holdout", generator=generator)["cases"])
        for alteration in ("holdout", "rename", "omit"):
            altered = book()
            case = altered["cases"][0]
            altered["cases"] = [case]
            if alteration == "holdout":
                case["split"] = altered["split"] = "holdout"
            elif alteration == "rename":
                case["source_group"] = "fos:DRN1161374"
            else:
                case.pop("source_group")
            case.pop("case_id")
            case["case_id"] = digest([altered["version"], case])
            with self.assertRaises(ValueError):
                validate(altered)

    def test_wrong_address_guessed_diagnosis_and_blanket_deferral(self):
        for case in [c for c in book()["cases"] if c["difficulty"] == "hard" and c["behaviour"] == "cooperative"]:
            w = case["world"]
            route = actions(case)
            stale = ToolCall("order_replacement", {"order_id": w["order"], "instruction_id": w["instructions"][0]["id"],
                                                  "destination": w["original_destination"]})
            for steps in ([*route[:-1], stale, route[-1]], [*route[1:-1], route[0], route[-1]],
                          [*route[:-2], Reply("Please contact another team.")]):
                self.assertEqual("FAIL", execute(case, steps).dimensions["operational"])
            if case["variant"] == declined_card.OUTCOMES[0]:
                retry = [*route[:-1], route[-2], route[-1]]
                self.assertEqual("PASS", execute(case, retry).dimensions["operational"])
