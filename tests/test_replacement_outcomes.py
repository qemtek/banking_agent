import unittest

from bank_demo.replacement_outcomes import CORRECT, INCORRECT, UNKNOWN, grade_turn


class ReplacementOutcomeTests(unittest.TestCase):
    def grade(self, reply, *, attachment="", saved=True, lookup=True, customer="Please send the issuer receipt."):
        receipt = "CS-12345"
        case = {"family": "card_reissue", "world": {"order": "ORD-56789", "receipts": {"replacement": receipt}},
                "messages": [customer], "timeout_return": True}
        events = [{"event_type": "model.requested", "payload": {"messages": [{"content": ""}]}}]
        if lookup:
            value = {"order": "ORD-56789", "receipts": {"replacement": receipt} if saved else {}}
            events.append({"event_type": "tool.completed", "component": "read_servicing_order", "payload": {"value": value}})
        facts = True, ("ADDR-54321", receipt) if saved else None, "ADDR-54321"
        return grade_turn(case, 1, customer, attachment + reply, reply, events, [], facts, set())["verdict"]

    def test_same_outcome_in_different_words(self):
        for reply in ("A replacement has already been requested. Your issuer receipt is CS-12345.",
                      "Your replacement for order ORD-56789 is recorded. The reference is CS-12345."):
            self.assertEqual(self.grade(reply), CORRECT)

    def test_wrong_reference_and_premature_claim(self):
        self.assertEqual(self.grade("The issuer receipt is ORD-56789."), INCORRECT)
        self.assertEqual(self.grade("Your replacement has been ordered.", saved=False), INCORRECT)
        self.assertEqual(self.grade("The issuer receipt is CS-12345.", saved=False), INCORRECT)

    def test_attachment_does_not_settle_surrounding_prose(self):
        attachment = "Order ORD-56789 · replacement · receipt CS-12345\n\n"
        for reply in ("Your card has not been ordered.", "Your issuer receipt is CS-99999.",
                      "It will arrive tomorrow."):
            self.assertEqual(self.grade(reply, attachment=attachment), UNKNOWN)

    def test_actual_request_and_issuer_observation(self):
        self.assertEqual(self.grade("You're welcome!", customer="Thank you, that answers my question."), CORRECT)
        self.assertEqual(self.grade("You're welcome!", customer="Thanks. Could you check the saved order again?",
                                    lookup=False), INCORRECT)
        self.assertEqual(self.grade("Your replacement is recorded. Your receipt is CS-12345.", lookup=False), UNKNOWN)
