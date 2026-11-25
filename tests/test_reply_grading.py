import unittest
from datetime import date
from decimal import Decimal

from bank_demo.evidence_tasks import explains_conflicting_credit, reports_outstanding
from bank_demo.routing_tasks import deadline_guidance, requests_detail


class ReplyGradingTests(unittest.TestCase):
    def test_date_of_next_step(self):
        day = date(2026, 9, 8)
        self.assertTrue(deadline_guidance("Please check your statement again after 8 September.", day, day))
        self.assertTrue(deadline_guidance("Check again tomorrow.", day, day))
        for reply in ("Deadline: 8 September. Check after 7 September.",
                      "Check after 18 September.", "Check after 8 September 2027."):
            self.assertFalse(deadline_guidance(reply, day, day), reply)

    def test_amount_owed(self):
        zero = Decimal(0)
        self.assertTrue(reports_outstanding("A £50.17 refund posted, so none of the amount remains outstanding.", zero))
        self.assertFalse(reports_outstanding("A £50.17 refund posted, but £50.17 is still outstanding.", zero))
        self.assertFalse(reports_outstanding("None remains outstanding. Actually £50.17 remains outstanding.", zero))

    def test_sources_disagree(self):
        claim = "The merchant says it credited £64.07. "
        self.assertTrue(explains_conflicting_credit(claim + "The credit is linked to a different purchase.", "ABC"))
        for reply in (claim + "The bank confirms that the credit is linked to this charge.",
                      "The merchant did not issue a credit. The bank found no matching credit.",
                      claim + "It is not linked to a different charge."):
            self.assertFalse(explains_conflicting_credit(reply, "ABC"), reply)

    def test_customer_is_asked_for_the_reference(self):
        self.assertTrue(requests_detail("Please ask the merchant for the trace reference and share it.", "trace reference"))
        self.assertFalse(requests_detail("The merchant may have a trace reference.", "trace reference"))
