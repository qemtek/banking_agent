import json
import tempfile
import unittest
from itertools import groupby
from pathlib import Path

from bank_demo.clarification_returns import DECISIONS, clarification_task, generate
from evaluation import evaluate
from harness import Reply, ToolCall
from test_servicing_tasks import Recorded, script


def reference(case):
    actions = script(case)
    start = next(i for i, action in enumerate(actions) if isinstance(action, Reply)) + 1
    actions.insert(start, ToolCall("read_servicing_permissions", {}))
    return actions


def run(case, actions):
    with tempfile.TemporaryDirectory() as tmp:
        return evaluate(clarification_task(case, lambda: Recorded(actions)), Path(tmp))


class ClarificationReturnsTests(unittest.TestCase):
    def test_shared_opening_and_all_three_decisions(self):
        cases = generate(19, 1)["cases"]
        for _, items in groupby(sorted(cases, key=lambda c: c["family"]), key=lambda c: c["family"]):
            trio = list(items)
            self.assertEqual({c["clarification_decision"] for c in trio}, set(DECISIONS))
            comparable = [{k:v for k,v in c.items() if k not in ("case_id", "clarification_decision")} for c in trio]
            self.assertEqual(len({json.dumps(c, sort_keys=True) for c in comparable}), 1)
            for c in trio:
                self.assertEqual(run(c, reference(c)).dimensions["operational"], "PASS")

    def test_empty_duplicate_wrong_target_and_denied_attempts(self):
        cases = generate(27, 1)["cases"]
        for c in cases:
            w = c["world"]
            name, target = w["scope"][0]
            action = ToolCall(name, {"reference": target})
            original = reference(c)
            if c["clarification_decision"] == "confirmed":
                index = original.index(action)
                alternatives = [[], [action, action], [ToolCall(name, {"reference": w["other"]})]]
                changes = [original[:index] + seq + original[index+1:] for seq in alternatives]
            else:
                changes = [original[:-1] + [action, original[-1]]]
            for actions in changes:
                result = run(c, actions)
                failures = {ch.category for ch in result.checks if ch.passed is False}
                self.assertIn("action_attempts", failures)
                if c["clarification_decision"] != "confirmed":
                    self.assertIn("access", failures)
                    self.assertNotIn("bank_writes", failures)
