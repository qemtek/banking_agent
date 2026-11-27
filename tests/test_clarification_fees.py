import unittest
from copy import deepcopy

from bank_demo.clarification_fees import generate
from harness import Reply, ToolCall
from test_clarification_returns import reference, run


class ClarificationFeeTests(unittest.TestCase):
    def setUp(self):
        self.cases = generate(90, 1)["cases"]

    @staticmethod
    def quote(case):
        terms = case["world"]["policy"]
        return ToolCall("send_servicing_notice", {"reference": "unresolved", "finding": terms["fee_basis"],
                                                 "amount_pence": str(terms["service_fee_pence"])})

    def test_matched_factors_and_reference_decisions(self):
        for family in ("payment_instruction", "cheque_service"):
            siblings = [c for c in self.cases if c["family"] == family]
            self.assertEqual(len(siblings), 4)
            fixed = []
            for c in siblings:
                w = deepcopy(c["world"])
                w.pop("policy")
                fixed.append(w)
                a = reference(c)
                first_reply = next(i for i, act in enumerate(a) if isinstance(act, Reply))
                if c["fee_enquiry"]:
                    a.insert(first_reply, self.quote(c))
                else:
                    a.pop(0)  # Directory evidence suffices for the neutral opening.
                self.assertEqual(run(c, a).dimensions["operational"], "PASS")
            self.assertTrue(all(w == fixed[0] for w in fixed))
            for intent in (False, True):
                low, high = sorted((c for c in siblings if c["fee_enquiry"] == intent),
                                   key=lambda c: c["world"]["policy"]["service_fee_pence"])
                self.assertEqual(low["messages"], high["messages"])
                self.assertEqual(low["world"]["policy"]["service_fee_pence"], 0)
                self.assertGreater(high["world"]["policy"]["service_fee_pence"], high["fee_cap_pence"])

    def test_mixed_reply_timing_and_correctness_are_independent(self):
        case = next(c for c in self.cases if c["fee_enquiry"])
        actions = reference(case)
        boundary = next(i for i, act in enumerate(actions) if isinstance(act, Reply))
        quote = self.quote(case)
        read = actions.pop(0)
        # The first notice asks for the account. Policy after that notice but
        # before publishing the fee is sufficient. A later read cannot repair a quote.
        actions[boundary-1:boundary-1] = [read, quote]
        self.assertEqual(run(case, actions).dimensions["operational"], "PASS")
        actions[boundary-1:boundary+1] = [quote, read]
        failed = {c.category for c in run(case, actions).checks if c.passed is False}
        self.assertEqual(failed, {"evidence"})
        for field, wrong in (("finding", "fee_per_instruction" if quote.arguments["finding"] == "fee_per_instrument"
                              else "fee_per_instrument"), ("amount_pence", "99999")):
            incorrect = ToolCall(quote.name, {**quote.arguments, field: wrong})
            actions[boundary-1:boundary+1] = [read, incorrect]
            failed = {c.category for c in run(case, actions).checks if c.passed is False}
            self.assertEqual(failed, {"outcome_delivery"})

    def test_blocked_over_cap_request_is_an_unauthorized_attempt(self):
        c = next(c for c in self.cases if not c["fee_enquiry"] and c["world"]["policy"]["service_fee_pence"] > 0)
        actions = reference(c)
        op, ref = c["world"]["scope"][0]
        actions.insert(-2, ToolCall(op, {"reference": ref}))
        failures = {k.category for k in run(c, actions).checks if k.passed is False}
        self.assertTrue({"access", "action_attempts"} <= failures)
        self.assertNotIn("bank_writes", failures)

    def test_a_correct_write_before_reading_policy_still_fails(self):
        case = next(c for c in self.cases if not c["fee_enquiry"] and c["world"]["policy"]["service_fee_pence"] == 0)
        actions = [a for a in reference(case) if getattr(a, "name", None) != "read_servicing_policy"]
        pos = next(i for i, a in enumerate(actions) if getattr(a, "name", None) == case["world"]["scope"][0][0])
        actions.insert(pos + 1, ToolCall("read_servicing_policy", {}))
        failures = {c.category for c in run(case, actions).checks if c.passed is False}
        self.assertEqual(failures, {"evidence"})

    def test_reading_the_fee_suffices_to_decline_a_chargeable_change(self):
        case = next(c for c in self.cases if not c["fee_enquiry"] and c["world"]["policy"]["service_fee_pence"] > 0)
        actions = reference(case)
        first = next(i for i, a in enumerate(actions) if isinstance(a, Reply))
        # The only first-turn bank action is a request for the account. The identified
        # record and tariff are read on return. The bank makes no claim about signing.
        actions = [*actions[first-1:first+1], *(a for a in actions[first+1:] if getattr(a, "name", None) != "read_servicing_permissions")]
        self.assertEqual(run(case, actions).dimensions["operational"], "PASS")
        without_record = [a for a in actions if getattr(a, "name", None) != "read_servicing_record"]
        self.assertIn("evidence", {c.category for c in run(case, without_record).checks if c.passed is False})
