from decimal import Decimal

from harness import Tool


class ReviewDesk:
    """In-memory card review for one synthetic account, with one case per scenario."""

    def __init__(self, customer, case_id, charges):
        self.customer = customer
        self.case_id = case_id
        self.charges = {row["reference"]: dict(row) for row in charges}
        self.credit = None
        self.items = set()
        self.status = "none"
        self.revisions = []

    def _authorize(self, session):
        if session.customer_id != self.customer:
            raise PermissionError("Account mismatch")

    def transactions(self, session):
        self._authorize(session)
        rows = [{key: row[key] for key in ("reference", "merchant", "amount")} for row in self.charges.values()]
        if self.credit:
            rows.append(self.credit.copy())
        return {"currency": "GBP", "transactions": rows}

    def receipt(self, session, reference):
        self._authorize(session)
        return dict(self.charges[reference])

    def snapshot(self):
        total = sum((Decimal(self.charges[ref]["amount"]) for ref in self.items), Decimal("0.00"))
        return {"case_id": self.case_id if self.status != "none" else None, "status": self.status,
                "references": sorted(self.items), "charge_total_gbp": str(abs(total))}

    def read(self, session):
        self._authorize(session)
        return self.snapshot()

    def edit(self, session, reference, operation):
        self._authorize(session)
        if reference not in self.charges or self.status not in ("none", "draft"):
            raise ValueError("Charge unavailable for editing")
        if operation == "add":
            self.items.add(reference)
        elif operation == "remove" and reference in self.items:
            self.items.remove(reference)
        else:
            raise ValueError("Invalid case edit")
        self.status = "draft"
        self.revisions.append((operation, reference))
        return self.snapshot()

    def finish(self, session, decision):
        self._authorize(session)
        if self.status != "draft" or not self.items:
            raise ValueError("An open draft with at least one charge is required")
        if decision not in ("submit", "withdraw"):
            raise ValueError("Unknown case decision")
        self.status = {"submit": "submitted", "withdraw": "withdrawn"}[decision]
        self.revisions.append((decision, self.case_id))
        return self.snapshot()

    def tools(self):
        return review_tools(self)


def review_tools(desk):
    return {
        "get_card_receipt": Tool("Read merchant receipt details for one transaction reference.",
                                 desk.receipt, parameters=("reference",)),
        "read_card_review": Tool("Read the current saved review status, charge references and total.", desk.read),
        "edit_card_review": Tool("Add or remove a charge in the customer's draft. operation is add or remove. "
                                 "The first add opens the draft.", desk.edit, parameters=("reference", "operation")),
        "finish_card_review": Tool("After the customer decides, finalize the draft. decision is submit or withdraw.",
                                   desk.finish, parameters=("decision",)),
    }
