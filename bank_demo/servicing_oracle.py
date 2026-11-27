"""Reference outcomes computed from the frozen case, independent of the servicing write dispatcher."""
from copy import deepcopy
from dataclasses import dataclass

from .servicing_rules import finance_quote
from .servicing_worlds import monthly_payment


@dataclass
class Expected:
    rows: dict
    operations: list
    finding: str
    amount: int
    evidence: set
    details: str


def expected_service(case, turn, *, current_outcome=False):
    w, family = case["world"], case["family"]
    initial = deepcopy(w["records"])
    if turn >= 2:
        for key, event in w["later"].items():
            initial[key].update(event)
    key, source, dest = w["subject"], w["primary"], w["other"]
    r = initial[key]
    rows = deepcopy(initial)
    ops = []
    scope = {op: ref for op, ref in w["scope"]}
    evidence = {key}
    amount, finding, details = 0, "ready", ""
    new = w["new_ref"]
    final = turn == 2 or not w["scope"] or current_outcome
    policy = w["policy"]
    if turn == 2 and case.get("clarification_decision") in ("pending", "withdrawn"):
        status = case["clarification_decision"]
        return Expected(initial, [], status, r["amount"], evidence, f"The app decision is {status}. No change is authorised.")

    if turn == 2 and "fee_enquiry" in case and policy["service_fee_pence"] > case["fee_cap_pence"]:
        return Expected(initial, [], "over_cap", policy["service_fee_pence"], evidence,
                        "The service fee exceeds the customer’s cap. Leave the instrument unchanged.")

    def commit(op, ref, **updates):
        ops.append([op, ref])
        rows[ref].update(updates)

    def request(op, **fields):
        commit(op, key)
        rows[new] = {"kind": "request", "owner": "alice", "label": op, "status": "pending", "target": key, **fields}

    if family == "payment_instruction":
        if not scope:
            evidence.add(r["account"])
            amount = max(0, r["amount"] - initial[r["account"]]["available"])
            finding = "shortfall" if amount else "covered"
        elif "skip_payment" in scope:
            amount = r["amount"]
            if r["status"] == "released":
                finding = "released"
            else:
                finding = "skipped"
                commit("skip_payment", key, status="skipped")
        elif "schedule_payment" in scope:
            amount, finding = w["amount"], "scheduled"
            commit("schedule_payment", key)
            rows[new] = {"kind": "payment", "owner": "alice", "label": r["label"], "status": "scheduled",
                         "series": None, "due": r["due"], "amount": amount, "account": source}
        elif "request_cancellation" in scope:
            finding = "requested"
            request("request_cancellation")
        else:
            amend = "amend_series" in scope
            amount, finding = (w["amount"] + 700, "amended") if amend else (0, "cancelled")
            updates = {"amount": amount} if amend else {"status": "cancelled"}
            commit("amend_series" if amend else "cancel_series", key, **updates)
            for ref, item in initial.items():
                if item.get("series") == key and item["status"] == "scheduled":
                    rows[ref].update(updates)
    elif family == "cheque_service":
        if "order_cheque_book" in scope:
            finding = "requested"
            request("order_cheque_book", destination=r["delivery"])
        else:
            finding, amount = r["status"], r["amount"]
            if finding == "issued":
                finding = "stopped"
                commit("stop_cheque", key, status="stopped")
            details = r.get("prior_stop", "")
    elif family == "account_lifecycle":
        eligible = False
        if "open_savings" in scope:
            threshold = r["minimum_tenure_days"]
            eligible = any(x["owner"] == "alice" and x["kind"] == "account" and x["status"] == "open"
                           and x["age_days"] >= threshold for x in initial.values())
            if eligible:
                commit("open_savings", key)
                rows[new] = {"kind": "savings", "owner": "alice", "label": r["label"], "product": key,
                             "status": "open", "available": 0}
            finding = "opened" if eligible else "ineligible"
            if not eligible:
                details = f"Opening requires an open current account at least {threshold} days old. None qualifies."
                if policy.get("close_requires_savings"):
                    details += (" The customer made closure conditional on opening savings. "
                                "The old account remains open for that reason.")
        if "close_current" in scope and ("open_savings" not in scope or eligible):
            evidence.add(source)
            src = initial[source]
            if src["holds"]:
                finding = "blocked"
            else:
                evidence.add(dest)
                amount = src["available"] - src["closure_fee"]
                commit("close_current", source, status="closed", available=0, fee_paid=src["closure_fee"])
                rows[dest]["available"] += amount
                finding = "opened_and_closed" if eligible else "closed"
                details = f"Closure fee {src['closure_fee']} pence. Sweep {amount} pence to Household."
    elif family == "term_deposit":
        if r["kind"] == "offer":
            # Funding determines booking eligibility. It does not affect a maturity projection.
            if scope and final:
                evidence.add(source)
            amount = finance_quote(r)
            finding = "quoted"
            if "book_deposit" in scope:
                if initial[source]["available"] - r["principal"] < r["reserve"]:
                    finding = "insufficient"
                else:
                    finding = "booked"
                    commit("book_deposit", key)
                    rows[source]["available"] -= r["principal"]
                    rows[new] = {"kind": "deposit", "owner": "alice", "label": r["label"], "status": "active",
                                 "offer": key, "account": source, "principal": r["principal"], "maturity": amount,
                                 "remaining_contributions": 11 if r["type"] == "recurring" else 0}
        elif "change_renewal" in scope:
            finding = "renewal_changed"
            commit("change_renewal", key, renewal="pay_out")
        else:
            amount = r["principal"] + r["accrued"] - r["penalty"]
            if amount >= policy["minimum_payout"]:
                finding = "closed"
                commit("redeem_deposit", key, status="closed", payout=amount)
                rows[source]["available"] += amount
            else:
                finding = "below_minimum"
    elif family == "loan_service":
        if case["variant"] == "settlement_fee_estimate":
            effective = max((d for d in w["policy_editions"] if d["effective_from"] <= w["as_of"]),
                            key=lambda d: d["effective_from"])
            waived = r["principal"] <= effective["terms"]["principal_ceiling_pence"]
            amount = r["principal"] + r["accrued"] + (0 if waived else r["charge"])
            finding = "estimate"
            details = "The settlement charge is waived." if waived else "The full settlement charge applies."
        elif not scope:
            illustration = case["variant"] == "monthly_illustration"
            finding = "illustrated" if illustration else "due"
            amount = monthly_payment(r["principal"], r["annual_bp"], r["months"]) if illustration else r["instalment"]
            details = f"Next scheduled due date {r['due']}." if not illustration else "Monthly illustration in GBP."
        elif "pay_instalment" in scope:
            finding, amount = r["collection"], r["instalment"]
            if finding == "due":
                interest = min(amount, r["accrued"])
                finding = "paid"
                commit("pay_instalment", key, collection="paid", principal=r["principal"] - amount + interest,
                       accrued=r["accrued"] - interest,
                       last_allocation={"principal": amount-interest, "interest": interest, "fee": 0})
                rows[source]["available"] -= amount
        else:
            amount = r["principal"] + r["accrued"] + r["charge"]
            if amount > policy["settlement_cap"]:
                finding = "over_cap"
            elif "request_settlement" in scope:
                finding = "requested"
                request("request_settlement")
            else:
                finding = "settled"
                commit("settle_loan", key, status="settled", collection="ended", principal=0, accrued=0, charge=0,
                       last_allocation={"principal":r["principal"], "interest":r["accrued"], "fee":r["charge"]})
                rows[source]["available"] -= amount
    elif family == "atm_fee":
        activity = [x for x in initial.values() if x.get("kind") == "withdrawal" and x["account"] == key
                    and x["day"][:7] == policy["period"]]
        charged = sum(x["bank_fee"]-x["reversal"] for x in activity)
        due = max(0, sum(x["network"] == "partner" for x in activity)-policy["free"])*policy["fee"]
        amount = max(0, charged - due - r["prior_adjustment"])
        details = f"Net bank charges {charged} pence. Correct bank fees {due} pence. Outstanding {amount} pence."
        finding = "reconciled"
        if scope and amount:
            finding = "credited"
            commit("credit_atm_fees", key, prior_adjustment=r["prior_adjustment"]+amount, available=r["available"]+amount)
    else:
        card = next(ref for ref, x in initial.items() if x["kind"] == "card" and x["label"] == "Travel")
        # The charge identifies its own card. Only the card whose limit is requested
        # needs separate eligibility evidence.
        evidence.add(card)
        cc = initial[card]
        amount = policy["requested_limit"]
        finding = "disputed_only"
        if r["urgent"]:
            commit("freeze_card", card, frozen=True)
            finding = "protected_and_disputed"
        elif "request_limit" in scope and not cc["pending_disputes"]:
            if cc["decision"] == "approve":
                commit("request_limit", card, limit=amount, limit_request="approved")
                finding = "approved_and_disputed"
            else:
                commit("request_limit", card, limit_request="pending")
                finding = "referred_and_disputed"
        if finding in ("disputed_only", "protected_and_disputed"):
            amount = cc["limit"]
        commit("file_dispute", key, disputed=True)
        rows[r["card"]]["pending_disputes"] += 1
        details = f"Disputed amount {r['amount']} pence. {'Requested' if finding == 'referred_and_disputed' else 'Current'} limit {amount} pence."
    if not final:
        if w["flow"] == "clarify":
            return Expected(initial, [], "clarify", 0, set(), "Identify which account the customer intends before acting.")
        if family == "dispute_credit_limit":
            amount = policy["requested_limit"]
            details = "Explain the conflict and the available processing order."
        return Expected(initial, [], "estimate", amount, evidence, details)
    return Expected(rows, ops, finding, amount, evidence, details)
