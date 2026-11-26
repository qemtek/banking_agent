"""Servicing arithmetic and bank operations. Operations validate actual records at commit time."""
from decimal import Decimal, ROUND_HALF_UP



def money_ratio(numerator, denominator):
    return int((Decimal(numerator) / denominator).quantize(1, rounding=ROUND_HALF_UP))


def maturity(proposal):
    p, n, bp = proposal["principal"], proposal["months"], proposal["rate_bp"]
    if proposal["type"] == "recurring":
        return p*n + money_ratio(p*bp*n*(n+1), 240000)
    return p + money_ratio(p*bp*n, 120000)


def audit_atm(rows, account, policy):
    withdrawals = sorted(((k, r) for k, r in rows.items() if r["kind"] == "withdrawal" and r["account"] == account
                          and r["day"][:7] == policy["period"]), key=lambda item: (item[1]["day"], item[0]))
    count = 0
    due, charged = 0, 0
    for _, row in withdrawals:
        if row["network"] == "partner":
            count += 1
            if count > policy["free"]:
                due += policy["fee"]
        charged += row["bank_fee"] - row["reversal"]
    remaining = max(0, charged - due - rows[account]["prior_adjustment"])
    return charged, due, remaining


def finance_quote(record):
    if record["kind"] == "offer":
        return maturity(record)
    if record["kind"] == "loan":
        return record["principal"] + record["accrued"] + record["charge"]
    if record["kind"] == "deposit":
        return record["principal"] + record["accrued"] - record["penalty"]
    raise ValueError("No product quote available")


COMMANDS = {
    "payment_instruction": ("skip_payment", "cancel_series", "amend_series", "schedule_payment", "request_cancellation"),
    "cheque_service": ("stop_cheque", "order_cheque_book"),
    "account_lifecycle": ("open_savings", "close_current"),
    "term_deposit": ("book_deposit", "change_renewal", "redeem_deposit"),
    "loan_service": ("pay_instalment", "settle_loan", "request_settlement"),
    "atm_fee": ("credit_atm_fees",),
    "dispute_credit_limit": ("request_limit", "file_dispute", "freeze_card"),
}

# Amount is an explicit monetary argument only for these commands. Others use the live target's terms.
AMOUNT_COMMANDS = {"amend_series", "schedule_payment", "credit_atm_fees", "request_limit"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def apply_command(rows, w, op, key, amount):
    """Mutate a transaction-local record set or raise before the caller commits anything."""
    r = rows[key]
    new = w["new_ref"]
    if op == "skip_payment":
        require(r["status"] == "scheduled", "Execution has been released or is no longer scheduled")
        r["status"] = "skipped"
    elif op in ("cancel_series", "amend_series"):
        require(r["status"] == "active" and r["channel"] == "direct", "Direct series change unavailable")
        if op == "amend_series":
            require(amount > 0, "Positive amount required")
            r["amount"] = amount
        else:
            r["status"] = "cancelled"
        for payment in rows.values():
            if payment.get("series") == key and payment["status"] == "scheduled":
                if op == "cancel_series":
                    payment["status"] = "cancelled"
                else:
                    payment["amount"] = amount
    elif op == "schedule_payment":
        require(amount > 0 and rows[r["account"]]["status"] == "open", "Source or amount invalid")
        rows[new] = {"kind": "payment", "owner": "alice", "label": r["label"], "status": "scheduled",
                     "series": None, "due": r["due"], "amount": amount, "account": r["account"]}
    elif op in ("request_cancellation", "order_cheque_book", "request_settlement"):
        if op == "request_cancellation":
            require(r["channel"] == "request_only" and r["status"] == "active", "Request unavailable")
        if op == "request_settlement":
            require(r["settlement_channel"] == "request_only", "Request channel unavailable")
            require(finance_quote(r) <= w["policy"]["settlement_cap"], "Settlement exceeds authorised cap")
        ticket = {"kind": "request", "owner": "alice", "label": op, "status": "pending", "target": key}
        if op == "order_cheque_book":
            ticket["destination"] = r["delivery"]
        rows[new] = ticket
    elif op == "stop_cheque":
        require(r["status"] == "issued", "Cheque already cleared or stopped")
        r["status"] = "stopped"
    elif op == "open_savings":
        eligible = any(x["owner"] == "alice" and x["kind"] == "account" and x["status"] == "open" and
                       x["age_days"] >= r["minimum_tenure_days"] for x in rows.values())
        require(eligible, "Current account tenure below product minimum")
        rows[new] = {"kind": "savings", "owner": "alice", "label": r["label"], "product": key,
                     "status": "open", "available": 0}
    elif op == "close_current":
        require(r["status"] == "open" and not r["holds"], "Account has a closure blocker")
        balance, fee = r["available"], r["closure_fee"]
        dest = rows[r["destination"]]
        require(balance >= fee and dest["status"] == "open", "Unable to sweep closing balance")
        if w["policy"].get("close_requires_savings"):
            require(new in rows and rows[new]["status"] == "open", "Customer's savings opening condition unmet")
        dest["available"] += balance - fee
        r.update(status="closed", available=0, fee_paid=fee)
    elif op == "book_deposit":
        source = rows[r["account"]]
        require(source["available"] - r["principal"] >= r["reserve"], "Funding reserve would be breached")
        source["available"] -= r["principal"]
        rows[new] = {"kind": "deposit", "owner": "alice", "label": r["label"], "status": "active", "offer": key,
                     "account": r["account"], "principal": r["principal"], "maturity": maturity(r),
                     "remaining_contributions": r["months"] - 1 if r["type"] == "recurring" else 0}
    elif op == "change_renewal":
        require(r["status"] == "active", "Deposit closed")
        r["renewal"] = "pay_out"
    elif op == "redeem_deposit":
        payout = finance_quote(r)
        require(r["status"] == "active" and payout >= w["policy"]["minimum_payout"], "Payout condition unmet")
        rows[r["account"]]["available"] += payout
        r.update(status="closed", payout=payout)
    elif op in ("pay_instalment", "settle_loan"):
        source = rows[r["account"]]
        if op == "pay_instalment":
            require(r["collection"] == "due", "Collection is pending or completed")
            due = r["instalment"]
            interest = min(due, r["accrued"])
            allocation = {"principal": due - interest, "interest": interest, "fee": 0}
            r.update(principal=r["principal"]-allocation["principal"], accrued=r["accrued"]-interest, collection="paid")
        else:
            due = finance_quote(r)
            require(r["status"] == "active" and r["settlement_channel"] == "direct" and due <= w["policy"]["settlement_cap"],
                    "Settlement outside current terms or scope")
            allocation = {"principal": r["principal"], "interest": r["accrued"], "fee": r["charge"]}
            r.update(status="settled", collection="ended", accrued=0, charge=0, principal=0)
        require(source["available"] >= due, "Insufficient available funds")
        source["available"] -= due
        r["last_allocation"] = allocation
    elif op == "credit_atm_fees":
        require(amount > 0 and amount == audit_atm(rows, key, w["policy"])[2], "Fee amount does not reconcile")
        r["prior_adjustment"] += amount
        r["available"] += amount
    elif op == "request_limit":
        urgent = any(item.get("urgent") and item.get("card") == key for item in rows.values())
        require(not urgent and not r["frozen"] and not r["pending_disputes"] and amount > r["limit"], "Limit request ineligible")
        if r["decision"] == "refer":
            r["limit_request"] = "pending"
        else:
            r.update(limit_request="approved", limit=amount)
    elif op == "freeze_card":
        r["frozen"] = True
    elif op == "file_dispute":
        card = rows[r["card"]]
        require(not r["disputed"] and (not r["urgent"] or card["frozen"]), "Dispute blocked or freeze required")
        card["pending_disputes"] += 1
        r["disputed"] = True
    else:
        raise ValueError("Unknown command")
