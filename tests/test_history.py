import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from harness import ContextBudget, Harness, Reply, Tool, ToolCall
from harness.audit import LocalAudit
from harness.context import READ_RESULT, RunContext
from harness.middleware import Middleware, ModelChanges


class HistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_receipt_can_be_paged_across_turns_without_repeating_action(self):
        record = {"detail": "x" * 550, "reference": "RECEIPT-2147"}
        submit = Mock(return_value=record)
        model = Mock()
        agent = Harness(model, "Help the customer.", {"submit": Tool("Submit once", submit),
                        "status": Tool("Read current status", lambda session: {"state": "complete", "log": "p" * 780})},
                        LocalAudit(self.directory), context_budget=ContextBudget(window=2850, reserve=100))
        chat = agent.conversation("alice")
        model.respond.side_effect = [ToolCall("submit", {}), Reply("Submitted.")]
        self.assertEqual(chat.send("Submit this.").stop_reason, "final_reply")
        # The shared business object may change after submission.
        record["reference"] = "CHANGED"
        model.respond.side_effect = [ToolCall("status", {}), Reply("Complete.")]
        chat.send("Please check the status and keep this explanation. " + "a" * 680)
        pages = []
        def inspect(messages, tools):
            self.assertIn(READ_RESULT, tools)
            source = next(m for m in messages if m["role"] == "tool_summary" and m["name"] == "submit")
            offset = "0"
            if messages[-1].get("name") == READ_RESULT:
                page = messages[-1]["value"]
                pages.append(page["text"])
                offset = page["next_offset"]
                if offset is None:
                    return Reply(json.loads("".join(pages))["reference"])
            return ToolCall(READ_RESULT, {"call_id": source["call_id"], "offset": offset})

        model.respond.side_effect = inspect
        result = chat.send("Which receipt did you get for my original submission?")
        self.assertEqual(result.reply, "RECEIPT-2147")
        self.assertGreater(len(pages), 1)
        submit.assert_called_once()
        events = [json.loads(line) for line in (self.directory / (result.run_id + ".jsonl")).read_text().splitlines()]
        self.assertEqual(sum(e["event_type"] == "tool.completed" and e["component"] == READ_RESULT
                             for e in events), len(pages))

    def test_reader_scope_and_previous_pages_follow_visibility(self):
        from harness.context import request_bytes
        tool = Tool("Read a result", lambda: None, parameters=("call_id", "offset"))
        tools = {READ_RESULT: tool.specification(), "source": "lookup"}
        def pair(name, key, args, result):
            return [{"role": "assistant", "call_id": key, "tool_call": {"name": name, "arguments": args}},
                    {"role": "tool", "name": name, "call_id": key, "value": result}]
        messages = pair("source", "first", {}, {"secret": "A" * 1100})
        messages += pair("source", "last", {}, {"value": 2})
        ctx = RunContext(ContextBudget(window=request_bytes(messages, tools), reserve=300), {})
        audit = Mock()
        ctx.prepare_request(messages, tools, audit)
        page = ctx.read_result(None, "first", "0")
        self.assertIn("A" * 20, page["text"])
        self.assertIn("error", ctx.read_result(None, "another-session", "0"))
        self.assertIn("error", RunContext(ctx.budget, {}).read_result(None, "first", "0"))
        ctx.remember_page("page", "first")
        messages += pair(READ_RESULT, "page", {"call_id": "first", "offset": "0"}, page)
        messages[1]["value"] = {"redacted": True}
        visible, _ = ctx.prepare_request(messages, tools, audit)
        self.assertNotIn("AAAA", json.dumps(visible))
        visible, _ = ctx.prepare_request(messages[2:], tools, audit)
        self.assertNotIn("AAAA", json.dumps(visible))
        self.assertIn("error", ctx.read_result(None, "first", "0"))

    def test_oversized_receipt_is_read_in_the_same_turn(self):
        model = Mock()
        submit = Mock(return_value={"confirmation": "R-92", "detail": "x" * 3500})
        agent = Harness(model, "Help.", {"submit": Tool("Submit", submit)}, LocalAudit(self.directory),
                        context_budget=ContextBudget(window=2400, reserve=100))
        chat = agent.conversation("alice")
        def recover(messages, tools):
            if len(messages) == 2:
                return ToolCall("submit", {})
            if messages[-1].get("name") == READ_RESULT:
                self.assertIn("R-92", messages[-1]["value"]["text"])
                return Reply("R-92")
            marker = next(m for m in messages if m["role"] == "tool_summary")
            return ToolCall(READ_RESULT, {"call_id": marker["call_id"], "offset": "0"})
        model.respond.side_effect = recover
        self.assertEqual(chat.send("Submit this and give me the confirmation.").reply, "R-92")
        submit.assert_called_once()

    def test_summary_keeps_recent_turn_and_is_invalidated_by_middleware(self):
        class TranscriptFilter(Middleware):
            redact = False
            def before_model(self, session, state, messages, tools):
                if self.redact:
                    return ModelChanges(messages=[messages[0], *messages[-3:]])
                return ModelChanges()

        layer = TranscriptFilter()
        model = Mock()
        agent = Harness(model, "Instructions", {}, LocalAudit(self.directory), middleware=[layer],
                        context_budget=ContextBudget(window=1800, reserve=100))
        chat = agent.conversation("alice")
        summary_calls = []
        def answer(messages, tools):
            if messages[0]["content"].startswith("Summarize"):
                self.assertEqual(tools, {})
                self.assertIn("R-241", messages[1]["content"])
                summary_calls.append(messages)
                return Reply("Customer withdrew R-241. No submission.")
            if messages[-1]["content"].startswith("What"):
                self.assertEqual(messages[0]["content"], "Instructions")
                self.assertIn("withdrew R-241", messages[1]["content"])
                self.assertEqual(messages[-3]["content"], recent)
            return Reply("Recorded.")
        model.respond.side_effect = answer
        recent = "My recent question " + "r" * 440
        for turn in ["I authorize case R-241. " + "a" * 490, "Correction: withdraw R-241. " + "b" * 490,
                     recent, "What did I decide? " + "c" * 200]:
            self.assertEqual(chat.send(turn).stop_reason, "final_reply")
        self.assertEqual(len(summary_calls), 1)
        self.assertEqual(chat.send("Thanks").stop_reason, "final_reply")
        self.assertEqual(len(summary_calls), 1)
        layer.redact = True
        self.assertEqual(chat.send("Hello").stop_reason, "final_reply")
        self.assertNotIn("R-241", json.dumps(model.respond.call_args.args[0]))
