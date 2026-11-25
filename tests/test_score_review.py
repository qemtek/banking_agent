import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

from bank_demo.casebook import score_case
from bank_demo.instances import generate
from bank_demo.ledger import gbp
from bank_demo.rescore import rescore
from evaluation.casebooks import digest
from evaluation.review import apply, packet
from evaluation.scoring import SCORE_VERSION
from evaluation.uplift import compare
from harness import Reply, ToolCall


class ScoreReviewTests(unittest.TestCase):
    def test_semantic_disagreement_and_paired_coverage(self):
        book = generate(43, "development", ["lookup"], ["cooperative"], ["easy"], 1)
        case, = book["cases"]
        amount = gbp(case["world"]["balances"]["alice"])
        with tempfile.TemporaryDirectory() as scratch:
            arms = []
            for arm, correct in (("baseline", False), ("candidate", True)):
                directory = Path(scratch) / arm
                directory.mkdir()
                reply = (f"{amount} pounds are available." if correct else
                         f"It would be wrong to say you have GBP {amount} available.")
                model = Mock()
                model.respond.side_effect = [ToolCall("get_available_balance", {}),
                                             ToolCall("get_available_balance", {}), Reply(reply)]
                row = score_case(case, directory, "scripted", "", lambda: model)
                row.update({k: case[k] for k in ("world_seed", "case_id", "family", "variant", "behaviour", "difficulty")})
                row.update(repeat=1, customer_mode="scripted")
                manifest = dict(casebook_id=digest(book), suite="generated", arm=arm, split="development", repeats=1,
                                generator_version=book["version"], score_version=SCORE_VERSION, model="fixture",
                                customer_mode="scripted", actor_version=1, reasoning_effort="fixture")
                report = directory / "report.json"
                for path, value in ((report, [row]), (directory / "manifest.json", manifest),
                                    (directory / "casebook.json", book)):
                    path.write_text(json.dumps(value))
                self.assertEqual(row["dimensions"], {"operational": "PASS", "communication": "REVIEW"})
                replay, = rescore(directory)
                self.assertEqual(replay["checks"], row["checks"])
                self.assertEqual(replay["state_evidence"], ["turn snapshot"])
                labels = packet(report)
                criterion, = labels["items"][0]["criteria"]
                self.assertNotIn("wording_signal", criterion)
                criterion["label"] = "pass" if correct else "fail"
                changed = deepcopy(labels)
                changed["items"][0]["criteria"][0]["delivered"] = "edited"
                with self.assertRaises(ValueError):
                    apply(report, changed, "fixture")
                reviewed, stats = apply(report, labels, "fixture")
                self.assertEqual(stats["criterion_counts"]["false_fail" if correct else "false_pass"], 1)
                reviewed_path = directory / "reviewed.json"
                reviewed_path.write_text(json.dumps(reviewed))
                arms.append(reviewed_path)
            self.assertEqual(compare(*arms, "operational_correct")["uplift_pp"], 0)
            self.assertEqual(compare(*arms, "communication_correct")["uplift_pp"], 100)
            self.assertEqual(compare(*arms)["uplift_pp"], 100)
            pending = compare(*(p.parent for p in arms), "communication_correct")
            self.assertIsNone(pending["uplift_pp"])
            self.assertEqual(pending["excluded_cases"], 1)
