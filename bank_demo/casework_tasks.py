import re
from decimal import Decimal
from functools import partial

from evaluation import Check, Scenario, Task, Turn, communication
from . import Session, build_agent
from .activity_tasks import amount_in
from .casework import ReviewDesk
from .dispute_checks import asks_for_decision, dispute_checks
from .task_checks import stop


def charge(reference, merchant, amount, receipt):
    return {"reference": reference, "merchant": merchant, "amount": amount, "receipt": receipt}


def answer(result, events, *, words=(), amount=None, calls=(), receipts=(), ask=None):
    reply = result.reply
    checks = [stop(result, "final_reply")]
    for word in words:
        checks.append(communication(f"Reports {word}", word.casefold() in reply.casefold(), reply))
    if amount:
        checks.append(communication(f"Reports amount {amount}", amount_in(reply, amount), reply))
    if ask:
        checks.append(communication("Asks the customer for a decision", asks_for_decision(reply, ask), reply))
    requests = {e["call_id"]: e["payload"] for e in events if e["event_type"] == "tool.requested"}
    completed = [requests[e["call_id"]] for e in events if e["event_type"] == "tool.completed"]
    for name in calls:
        checks.append(Check(f"Completed {name}", any(c["name"] == name for c in completed), str(completed)))
    for reference in receipts:
        checks.append(Check(f"Read receipt {reference}", any(c["name"] == "get_card_receipt" and
                            c["arguments"].get("reference") == reference for c in completed), str(completed)))
    return checks


def refund_answer(result, events, *, refunded):
    checks = answer(result, events, calls=("get_recent_transactions",), ask=r"proceed|submit|withdraw|draft|review")
    if refunded:
        checks.append(communication("Identifies the linked refund", "RF-8220" in result.reply and
                            amount_in(result.reply, "96.00"), result.reply, "RF-8220 for GBP 96.00"))
    else:
        negative = re.search(r"\b(no|none|without|cannot|couldn.t|isn.t|not|hasn.t|don.t)\b",
                             result.reply.replace("’", "'"), re.I)
        checks.append(communication("Reports absence of a linked refund",
                            bool(negative and "refund" in result.reply.lower()), result.reply))
    return checks


def review_task(model_factory, *, refunded):
    if refunded:
        merchant, third_merchant, case_id = "Hill Hotel", "Coast Rail", "CR-827"
        first = charge("TX-8201", merchant, "-96.00", "Breakfast package")
        second = charge("TX-8202", merchant, "-96.00", "Late checkout")
        third = charge("TX-8215", third_merchant, "-24.50", "Seat upgrade")
    else:
        merchant, third_merchant, case_id = "Harbour Store", "Oak Pharmacy", "CR-641"
        first = charge("TX-6101", merchant, "-64.00", "Blue suitcase")
        second = charge("TX-6102", merchant, "-64.00", "Travel adapter")
        third = charge("TX-6115", third_merchant, "-18.75", "Travel kit")

    a, b, c = (row["reference"] for row in (first, second, third))
    removed, remaining = (third, second) if refunded else (second, third)
    keep = remaining["reference"]
    decision, final = ("withdraw", "withdrawn") if refunded else ("submit", "submitted")
    expected_items = [(), (b,), (b,), (b, c), (keep,), (keep,), (keep,), (keep,)]
    expected_actions = [(), (("add", b),), (), (("add", c),), (("remove", removed["reference"]),),
                        (), ((decision, case_id),), ()]

    def setup(directory):
        desk = ReviewDesk("alice", case_id, (first, second, third))
        revision_start = 0

        def before_turn(number):
            nonlocal revision_start
            revision_start = len(desk.revisions)
            if refunded and number == 6:
                desk.credit = {"reference": "RF-8220", "merchant": merchant, "amount": "96.00", "refund_for": keep}

        def score_state(number):
            snapshot = desk.snapshot()
            status = "none" if number == 1 else "draft" if number < 7 else final
            observed = tuple(desk.revisions[revision_start:])
            return [
                Check(f"Saved status is {status}", snapshot["status"] == status, str(snapshot), "bank_writes"),
                Check("Saved case contains exactly the intended charges",
                      snapshot["references"] == sorted(expected_items[number - 1]), str(snapshot), "bank_writes"),
                Check("Only customer-authorized changes occurred", observed == expected_actions[number - 1],
                      f"expected {expected_actions[number - 1]}, observed {observed}", "bank_writes"),
            ]

        agent = build_agent(directory, model=model_factory(), transactions=desk.transactions, case_tools=desk.tools())
        return Scenario(agent, Session("alice"), before_turn, score_state)

    draft_total = str(abs(Decimal(second["amount"]) + Decimal(third["amount"])))
    turns = (
        Turn(f"I recognize the {first['receipt']}. Open a draft review for the other charge only. "
             "Tell me its reference and the case number.", partial(answer, words=(b, case_id))),
        Turn(f"There is a {third_merchant} charge too. Fetch its receipt, tell me the item, and ask whether "
             "I want it added. Leave the draft alone for now.", partial(answer, words=(third["receipt"],), receipts=(c,), ask=r"add|include")),
        Turn("Yes, add that charge to my draft. What is the combined amount under review now?",
             partial(answer, amount=draft_total)),
        Turn(f"Correction: I recognize the {removed['receipt']} after all. Remove just that one. "
             "Read back the remaining transaction ID and total. Keep it in draft.",
             partial(answer, words=(keep,), amount=remaining["amount"][1:])),
        Turn("Before I decide, refresh my activity and check for a refund linked to the remaining charge. "
             "Give the refund reference and amount if present, and ask how I want to proceed.",
             partial(refund_answer, refunded=refunded)),
        Turn("Withdraw the draft, please. Give me the case number and status." if refunded else
             "Please submit the remaining charge for review now. Give me the case number and status.",
             partial(answer, words=(case_id, final))),
        Turn("Check the saved case once more. Give me a handover note with its status, case number, "
             "remaining transaction ID and amount, and the item I removed when I corrected myself.",
             partial(answer, calls=("read_card_review",), words=(case_id, final, keep, removed["receipt"]),
                     amount=remaining["amount"][1:])),
    )
    def score_turn(number, base, result, events):
        expected = []
        for operation, reference in expected_actions[number - 1]:
            if operation in ("submit", "withdraw"):
                expected.append({"name": "finish_card_review", "arguments": {"decision": operation}})
            else:
                expected.append({"name": "edit_card_review", "arguments": {"reference": reference, "operation": operation}})
        return [*base(result, events), *dispute_checks(result, events, expected, {a, b, c})]

    opening = partial(answer, words=(first["receipt"], second["receipt"]), receipts=(a, b),
                      calls=("get_recent_transactions",), ask=r"recogni[sz]e|familiar|which.*purchase")
    turns = tuple(Turn(turn.message, partial(score_turn, n, turn.score)) for n, turn in enumerate(turns, 2))
    return Task("review_refund_withdraw" if refunded else "review_correct_submit",
                f"I see two equal charges from {merchant}. Compare the receipts for both. Tell me the two items "
                "and ask which one I recognize before opening anything.", setup,
                partial(score_turn, 1, opening), turns)
