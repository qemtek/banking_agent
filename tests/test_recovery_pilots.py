import io
import json
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from dataclasses import replace

from adapters.codex_cli import create_model
from bank_demo.casebook import limited_model, main
from bank_demo.recovery_pilots import generate
from harness import Reply
from test_card_servicing_tasks import call, run


class RecoveryPilotTests(unittest.TestCase):
    def test_timeout_siblings_and_premature_retry(self):
        cases = generate("timeout", 182, 1)["cases"]
        a, b = cases
        self.assertEqual(a["messages"], b["messages"])
        self.assertEqual({k for k in a["world"] if a["world"][k] != b["world"][k]}, {"ack_failure"})
        for case in cases:
            lookup = call(case, "read_servicing_order")
            authority = call(case, "read_servicing_authority")
            retry = call(case, "replacement", op=True)
            reply = Reply(case["world"]["receipts"]["replacement"])
            absent = case["world"]["ack_failure"] == "before_commit"
            valid = [lookup, authority, retry, reply] if absent else [lookup, reply]
            self.assertEqual(run(case, valid).dimensions["operational"], "PASS")
            invalid = [authority, retry, lookup, reply]
            failed = {c.category for c in run(case, invalid).checks if c.passed is False}
            self.assertIn("evidence", failed)
            if not absent:
                self.assertIn("action_attempts", failed)

    def test_explicit_second_check_and_acknowledgement(self):
        for case in generate("verification", 183, 1)["cases"]:
            lookup = call(case, "read_servicing_order")
            reply = Reply(case["world"]["receipts"]["replacement"])
            actions = [lookup]
            if case["world"]["ack_failure"] == "before_commit":
                actions.extend([call(case, "read_servicing_authority"), call(case, "replacement", op=True)])
            without_lookup = run(case, actions + [reply, reply])
            if case["return_followup"] == "thanks":
                self.assertEqual(without_lookup.dimensions["operational"], "PASS")
            else:
                self.assertEqual(without_lookup.dimensions["operational"], "FAIL")
                self.assertEqual(
                    run(case, actions + [reply, lookup, reply]).dimensions["operational"], "PASS"
                )

    def test_budget_preserves_the_terra_dispatch_measure_and_reserve(self):
        model = limited_model(create_model, 9500)
        ordinary = create_model().context_budget
        self.assertEqual(model.context_budget, replace(ordinary, window=9500 + ordinary.reserve))
        with self.assertRaises(ValueError):
            limited_model(create_model, ordinary.window)

    def test_only_compaction_runner_accepts_an_override(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.json"
            path.write_text(json.dumps(generate("compaction", 73, 1)))
            args = [str(path), "--arm", "sample"]
            with (
                patch.object(sys, "argv", ["casebook", "run", *args, "--input-bytes", "9500"]),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 2)
            for mode, flags, expected in (
                ("run", [], None),
                ("run-compaction", ["--input-bytes", "9500"], 9500),
            ):
                with (
                    patch.object(sys, "argv", ["casebook", mode, *args, *flags]),
                    patch("bank_demo.casebook.run") as run_cases,
                ):
                    main()
                    self.assertEqual(getattr(run_cases.call_args.args[1], "input_bytes", None), expected)
