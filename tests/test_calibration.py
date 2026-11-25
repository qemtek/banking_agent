import json
import tempfile
import unittest
from pathlib import Path

from bank_demo.instances import generate
from evaluation.calibration import calibration
from evaluation.casebooks import digest


class CalibrationTests(unittest.TestCase):
    def test_timeout_retry_and_repeat_keep_one_primary_world(self):
        book = generate(2, "development", ["lookup"], ["cooperative"], ["easy"], 1)
        case = book["cases"][0]
        manifest = dict(suite="generated", seed=2, split="development", casebook_id=digest(book),
                        generator_version=book["version"], score_version=1, actor_version=1, customer_mode="llm",
                        instruction_hash=digest(""), bank_prompt_version=2, model="terra", reasoning_effort="medium",
                        repeats=1)
        with tempfile.TemporaryDirectory() as tmp:
            def save(name, status):
                path = Path(tmp) / name
                path.mkdir()
                row = {k: case[k] for k in ("case_id", "difficulty", "behaviour", "variant", "family", "world_seed")}
                row.update(status=status, repeat=1, checks=[], turn_audits=[])
                for file, content in (("casebook", book), ("manifest", manifest), ("report", [row])):
                    (path / (file + ".json")).write_text(json.dumps(content))
                return path
            initial = save("primary", "ERROR")
            retry = save("retry", "PASS")
            repeat = save("repeat", "FAIL")
            result = calibration([initial], [repeat], [retry])
            self.assertEqual(result["worlds"], 1)
            self.assertEqual(result["repeat_outcomes"], {"PASS to FAIL": 1})
            self.assertEqual([a["status"] for a in result["attempts"]["primary"]], ["ERROR", "PASS"])
            with self.assertRaisesRegex(ValueError, "distinct"):
                calibration([initial], [initial])
            with self.assertRaisesRegex(ValueError, "scored case"):
                calibration([repeat], [], [retry])
