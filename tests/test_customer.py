import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from bank_demo.customer import ClaimCustomer, CONSENT
from bank_demo.ledger import statement
from bank_demo.ledger_tasks import claim_tasks
from evaluation import evaluate
from harness import Reply, ToolCall


class ModelCustomerTests(unittest.TestCase):
    def test_roles_receive_only_their_own_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = statement(41)
            bank = Mock()
            first_reply = "I found your draft. Can you confirm the merchant?"
            bank.respond.side_effect = [
                ToolCall("read_card_review", {}), ToolCall("get_recent_transactions", {}), Reply(first_reply),
                ToolCall("finish_card_review", {"decision": "submit"}), Reply(f"Case {data.case} was submitted.")]
            customer_model = Mock(model_id="customer", reasoning_effort="medium")
            messages = ["I think I was refunded.", "The merchant is North Pier. " + CONSENT]
            customer_model.respond.side_effect = [Reply(t) for t in messages]
            customer = ClaimCustomer(customer_model, "mistaken", root / "customers")
            task = claim_tasks(lambda: bank, 41)[1]
            result = evaluate(task, root, customer=customer)
            self.assertFalse(result.error)
            self.assertEqual([m["text"] for m in result.dialogue][:3], [messages[0], first_reply, messages[1]])
            second, tools = customer_model.respond.call_args.args
            self.assertEqual(tools, {})
            self.assertEqual(second[1:], [{"role": "assistant", "content": messages[0]},
                                          {"role": "user", "content": first_reply}])
            for private in (data.case, data.target, data.foreign, "finish_card_review", "approval", task.name):
                self.assertNotIn(private, json.dumps(second))
            self.assertNotIn(customer.persona, str(bank.respond.call_args_list))
            self.assertEqual(len(customer.audit_paths), 2)
            for path, invocation in zip(customer.audit_paths, customer_model.respond.call_args_list):
                event = json.loads(Path(path).read_text().splitlines()[0])
                self.assertEqual(event["payload"]["messages"], invocation.args[0])

    def test_missing_conditional_instruction_stops_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            customer_model = Mock(model_id="customer", reasoning_effort="medium")
            customer_model.respond.side_effect = [Reply("Check my draft."), Reply("Thanks for checking.")]
            bank = Mock()
            bank.respond.return_value = Reply("Your draft is still open.")
            customer = ClaimCustomer(customer_model, "mistaken", root / "customers")
            result = evaluate(claim_tasks(lambda: bank, 41)[1], root, customer=customer)
            self.assertEqual(result.status, "ERROR")
            self.assertIn("Customer turn 2", result.error)
            self.assertEqual(len(result.turn_audits), 1)
            self.assertEqual(len(result.dialogue), 2)
            bank.respond.assert_called_once()

    def test_customer_failure_is_separate_from_bank_performance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = Mock(model_id="customer", reasoning_effort="medium")
            model.respond.return_value = ToolCall("inspect_ledger", {})
            bank = Mock()
            customer = ClaimCustomer(model, "misleading", root / "customers")
            result = evaluate(claim_tasks(lambda: bank, 41)[0], root, customer=customer)
            self.assertEqual(result.status, "ERROR")
            self.assertIn("Customer turn 1", result.error)
            self.assertEqual(result.dialogue, [])
            bank.respond.assert_not_called()
