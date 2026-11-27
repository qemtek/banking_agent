import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from adapters.codex_cli import CodexCLI
from bank_demo.large_history import expected, history, trial
from harness import Reply, ToolCall
from harness.usage import record_usage


class LargeHistoryTests(unittest.TestCase):
    def test_oracle_matches_independently_reconstructed_case_and_postings(self):
        for seed in (21, 22):
            for keep, linked in ((True, True), (False, True), (False, False)):
                archive, facts = history(seed, keep, linked)
                tools = [m for m in archive if m["role"] == "tool"]
                signatures = [m["value"] for m in tools if m["name"] == "read_signed_amendment"]
                self.assertEqual(signatures[1]["supersedes"], signatures[0]["reference"])
                register = next(m["value"] for m in tools if m["name"] == "read_case_register")
                chosen = next(r for r in register if r["invoice"] == signatures[1]["invoice"])
                postings = [
                    r
                    for m in tools
                    if m["name"] == "read_archived_statement"
                    for r in m["value"]["transactions"]
                ]
                credits = [r for r in postings if r.get("refund_for") == chosen["charge"]]
                wanted = expected(facts)
                self.assertEqual(len(postings), 1611)
                self.assertEqual(wanted["case_ref"], chosen["case"])
                self.assertEqual(wanted["authority_ref"], signatures[1]["reference"])
                self.assertEqual(wanted["credit_ref"], credits[0]["reference"] if credits else "none")
                self.assertEqual(wanted["decision"], "withdraw" if linked and not keep else "keep_open")

    def test_compression_is_measured_and_ordinary_dispatch_limit_is_preserved(self):
        _, facts = history(22, False, True)
        wanted = expected(facts)
        measured = []

        class Recorded(CodexCLI):
            def respond(self, messages, tools):
                measured.append(
                    (bool(tools), self.context_budget, self.context_budget._size(messages, tools))
                )
                record_usage(dict(input_tokens=100, output_tokens=10, cached_input_tokens=0))
                if not tools:
                    return Reply(json.dumps(wanted))
                return (
                    Reply("Recorded.")
                    if messages[-1]["role"] == "tool"
                    else ToolCall("record_resolution", wanted)
                )

        with TemporaryDirectory() as root, patch("bank_demo.large_history.create_model", Recorded):
            full = trial(Path(root), 22, False, True, "full")
            compact = trial(Path(root), 22, False, True, "compacted")
        self.assertEqual(full["history_digest"], compact["history_digest"])
        self.assertTrue(full["exact"] and compact["exact"])
        self.assertGreater(full["before"], 300_000)
        self.assertEqual(full["before"], full["after"])
        self.assertLess(compact["after"], full["before"] // 8)
        self.assertEqual((full["summary_calls"], compact["summary_calls"]), (0, 1))
        self.assertTrue(all(budget == CodexCLI.context_budget for _, budget, _ in measured))
        self.assertGreater(next(size for is_action, _, size in measured if not is_action), 300_000)
