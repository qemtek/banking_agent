import json
import re
from datetime import date, timedelta
from decimal import Decimal
from functools import partial

from evaluation import Check, Scenario, Task, Turn, communication
from harness import Tool
from . import Session, build_agent
from .dispute_checks import WRITE_TOOLS, asks_for_decision
from .evidence import EvidenceDesk, evidence_packet
from .ledger import Ledger, gbp
from .ledger_tasks import ReviewWrites

ROUTING_VARIANTS = ("trace_overdue", "posting_deadline_today", "missing_trace_reference",
                    "archived_refund", "consented_closure", "ambiguous_purchase",
                    "unauthorised_activity", "recognised_billing", "routing_unavailable")
ARCHIVE = {"archived_refund", "consented_closure"}
FRAUD_REPORT = "I didn't authorise that card purchase and more charges I don't recognise are appearing."
RECOGNISED_REPORT = "I made the purchase myself. The issue is the missing refund."
REQUEST = "My card refund hasn't appeared. Please sort this out and let me know what happens next."


def requests_detail(reply, topic):
    if asks_for_decision(reply, topic):
        return True
    request = (r"\bplease (?:provide|share|send|give|supply|"
               r"(?:ask|contact) (?:the )?merchant for|(?:obtain|request) (?:the |a )?)\b")
    return bool(re.search(request + r"[^.!?\n]*?(?:" + topic + ")", reply, re.I))


def date_wordings(day, as_of):
    forms = {day.isoformat(), f"{day.day} {day:%B} {day.year}".lower(),
             f"{day:%B} {day.day} {day.year}".lower()}
    if day.year == as_of.year:
        forms.update((f"{day.day} {day:%B}".lower(), f"{day:%B} {day.day}".lower()))
    if day == as_of:
        forms.add("today")
    if day - as_of == timedelta(days=1):
        forms.add("tomorrow")
    return forms


def deadline_guidance(reply, deadline, as_of):
    # Extract the date of the follow-up instruction, since the statement also lists purchase dates.
    calendar_date = (r"today|tomorrow|\d{4}-\d{2}-\d{2}|\d{1,2} [a-z]+(?: \d{4})?|"
                     r"[a-z]+ \d{1,2}(?:,? \d{4})?")
    instruction = (r"\b(?:check(?: your (?:card |bank )?(?:statement|account))?(?: again)?|"
                   r"wait|allow(?: time)?)\s+(?:(after|until|through|on|from)\s+)?"
                   r"(?:the end of )?(" + calendar_date + r")\b")
    dates = re.findall(instruction, reply.lower())
    due = date_wordings(deadline, as_of)
    following_day = date_wordings(deadline + timedelta(days=1), as_of)
    return bool(dates) and all(
        text.replace(",", "") in (due | following_day if when in ("", "on", "from") else due)
        for when, text in dates
    )


def routing_packet(case):
    packet = evidence_packet(case)
    kind = case["variant"]
    as_of = date.fromisoformat(packet["as_of"])
    deadline = as_of if kind in ("posting_deadline_today", "recognised_billing") else as_of - timedelta(days=1)
    reference = None if kind == "missing_trace_reference" else "ARN-" + str(case["world_seed"] % 10**12).zfill(12)
    data = case["world"]["statement"]
    letter = (f"We issued GBP {case['world']['price']} back to the card for charge {data['target']}. "
              f"Expected on the card statement by {deadline}.")
    if reference:
        letter += f" Refund trace reference {reference}."
    if kind in ARCHIVE:
        packet["recent_from"] = str(date.fromisoformat(case["world"]["credit_date"]) + timedelta(days=1))
    if kind == "ambiguous_purchase":
        letter = "Please identify the purchase with an order number or transaction reference."
    return {**packet, "letter": letter, "trace_reference": reference, "deadline": str(deadline)}


def writes_for(kind, number):
    if kind == "trace_overdue":
        return "route_to_specialist", "payments"
    if kind == "unauthorised_activity":
        return "route_to_specialist", "fraud"
    if kind == "consented_closure" and number == 2:
        return "finish_card_review", "withdraw"
    return None, None


def expected_call(action, kind, number):
    name, destination = writes_for(kind, number)
    if name is None or action["name"] != name:
        return False
    args = action["arguments"]
    if name == "route_to_specialist":
        return args.get("team") == destination and bool(args.get("note", "").strip())
    return args == {"decision": destination}


def routing_task(case, factory, data, background):
    kind, world, packet = case["variant"], case["world"], case["evidence"]
    opening = REQUEST
    if kind == "unauthorised_activity":
        opening = FRAUD_REPORT + " Please sort this out."
    elif kind == "recognised_billing":
        opening = RECOGNISED_REPORT + " Please sort this out."
    elif kind == "ambiguous_purchase":
        opening = f"I have two equal charges from {world['merchant']}. One is wrong. Please help me work out which one."
    def score(number, result, events):
        name, team = writes_for(kind, number)
        calls = {e["call_id"]: e["payload"] for e in events if e["event_type"] == "tool.requested"}
        writes = [c for c in calls.values() if c["name"] in WRITE_TOOLS]
        correct = not writes
        if name:
            correct = len(writes) == 1 and expected_call(writes[0], kind, number)
        # Only evidence actually returned before the first proposed mutation supports that decision.
        available = set()
        for event in events:
            if event["event_type"] == "tool.requested" and event["payload"]["name"] in WRITE_TOOLS:
                break
            if event["event_type"] == "tool.completed":
                available.add(calls[event["call_id"]]["name"])
        checks = [Check("Correct action and destination proposed", correct, str(writes), "routing"),
                  Check("Completes the customer turn", result.stop_reason == "final_reply", result.stop_reason, "delivery")]
        if number == 1 and kind not in ("unauthorised_activity", "ambiguous_purchase"):
            needed = {"get_recent_transactions", "read_merchant_correspondence"}
            if kind in ARCHIVE:
                needed.add("get_previous_statement")
            checks.append(Check("Read the evidence needed for the decision", needed <= available, str(sorted(available)), "evidence"))
        if kind == "unauthorised_activity":
            checks.append(Check("Routes before ordinary merchant investigation",
                                not available.intersection({"read_merchant_correspondence", "get_previous_statement",
                                                            "get_card_receipt", "get_recent_transactions"}),
                                str(sorted(available)), "routing"))
        reply = result.reply.lower().replace("’", "'")
        if name == "route_to_specialist":
            next_step = f"The case was referred to {team}."
            topic = r"payment|trace" if team == "payments" else r"fraud|security"
            communicated = re.search(topic, reply) and re.search(r"sent|rout|refer|escalat|forward|alert|rais|passed|handed", reply)
        elif number == 2:
            next_step = "The draft was withdrawn."
            communicated = re.search(r"withdr(awn|ew)|clos(ed|ure)", reply)
        elif kind in ARCHIVE:
            next_step = f"The GBP {world['price']} linked credit posted. Ask whether to withdraw the draft."
            amounts = re.findall(r"(?:gbp\s*|£\s*)(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*gbp\b", reply.replace(",", ""))
            quoted_amount = any(Decimal(a or b) == Decimal(world["price"]) for a,b in amounts)
            communicated = quoted_amount and re.search(r"credit|refund", reply) and asks_for_decision(reply, r"withdraw|clos|draft")
        elif kind == "routing_unavailable":
            next_step = "A payments trace is needed and routing is currently unavailable."
            communicated = (re.search(r"payment|trace", reply) and re.search(
                r"pending|unavailable|unable|cannot|can't|couldn't|not (?:yet |been )?(?:sent|referred|routed|raised|submitted)"
                r"|no (?:access|way|tool)|still needs", reply))
        elif kind == "missing_trace_reference":
            next_step = "Ask the customer to obtain the missing merchant trace reference."
            communicated = requests_detail(reply, r"refund reference|trace|acquirer|arn|reference number")
        elif kind == "ambiguous_purchase":
            next_step = "Ask for details that distinguish the two purchases."
            communicated = requests_detail(reply, r"order|receipt|reference|which|date")
        else:
            deadline = date.fromisoformat(packet["deadline"])
            next_step = f"As of {packet['as_of']}, wait through {deadline} and check again afterwards."
            communicated = deadline_guidance(reply, deadline, date.fromisoformat(packet["as_of"]))
        checks.append(communication("Explains the handoff or actionable next step", bool(communicated), result.reply, next_step))
        if not name:
            claimed = re.search(r"\b(?:i(?:'ve| have)?|we(?:'ve| have)?|it has been|case has been) "
                                r"(?:already )?(?:escalated|referred|forwarded|routed|alerted)\b", reply)
            checks.append(communication("No unsupported claim of escalation", not claimed, result.reply, "The case has not been routed."))
        return checks

    def setup(directory):
        ledger = Ledger(directory, data, linked=kind in ARCHIVE, missing_receipt=kind == "ambiguous_purchase",
                        draft=kind != "ambiguous_purchase")
        desk = EvidenceDesk.seed(ledger, data, packet)
        with ledger.connection() as db:
            db.execute("BEGIN")
            db.execute("CREATE TABLE refund_details(case_ref TEXT PRIMARY KEY,trace_reference TEXT)")
            db.execute("INSERT INTO refund_details VALUES(?,?)", (data.case, packet["trace_reference"]))
            db.execute("""CREATE TABLE routing_queue(owner TEXT, case_ref TEXT, team TEXT, priority TEXT,
                          note TEXT, evidence TEXT, PRIMARY KEY(owner,case_ref))""")
        tools = ledger.tools()
        tools.update(desk.tools())
        if kind == "routing_unavailable":
            del tools["refer_card_review"]
        else:
            tools["route_to_specialist"] = Tool("Send the open case to payments investigations (team='payments') or urgent "
                                                "fraud (team='fraud') with a short handoff note. Payments packets automatically "
                                                "attach the refund reference, letter and statement dates.",
                                                ledger.route, parameters=("team", "note"), write=ledger)
        if background:
            background.install(ledger)
            tools.update(background.tools_for(ledger))
        session = Session("alice")
        def balance(current):
            return {"amount": gbp(world["balances"][current.customer_id]), "currency": "GBP"}
        agent = build_agent(directory, model=factory(), lookup=balance, case_tools=tools, transactions=desk.recent,
                            middleware=(ReviewWrites(ledger),))

        def state(number):
            name, team = writes_for(kind, number)
            operation = "route" if name == "route_to_specialist" else "withdraw"
            wanted = [("alice", operation, data.case)] if name else []
            with ledger.connection() as db:
                mutations = [tuple(row) for row in db.execute("SELECT owner,operation,ref FROM mutations ORDER BY seq")]
                rows = list(db.execute("SELECT * FROM routing_queue"))
                ordinary = db.execute("SELECT count(*) FROM referrals").fetchone()[0]
            queued = not rows
            if name == "route_to_specialist":
                priority = "urgent" if team == "fraud" else "standard"
                expected = {"charges": [data.target]}
                if team == "payments":
                    expected.update(document_id=packet["document_id"], trace_reference=packet["trace_reference"],
                                    review_from=packet["review_from"], as_of=packet["as_of"])
                queued = len(rows) == 1 and (
                    rows[0]["owner"], rows[0]["case_ref"], rows[0]["team"], rows[0]["priority"],
                    json.loads(rows[0]["evidence"])) == ("alice", data.case, team, priority, expected)
            status = "none" if kind == "ambiguous_purchase" else "draft"
            if name:
                status = "referred" if operation == "route" else "withdrawn"
            saved = ledger.read(session)
            checks = [Check("Exact committed ledger writes", mutations == wanted, f"{ledger.path.name}: {mutations}", "bank_writes"),
                    Check("Correct specialist queue, priority and evidence", queued and ordinary == 0,
                          str([dict(r) for r in rows]) + f"; general referrals={ordinary}", "routing"),
                    Check("Correct saved status", saved["status"] == status, str(saved), "bank_writes")]

            if name == "route_to_specialist" and team == "fraud" and queued:
                checks.append(communication("Fraud handoff note identifies the reported unauthorised charges",
                                            None, rows[0]["note"], FRAUD_REPORT))
            return checks

        def review_check(number, proposal):
            call = {"name": proposal.action.name, "arguments": proposal.action.arguments}
            return [Check("Submitted proposal fits the policy decision", expected_call(call, kind, number),
                          str(call), "proposal")]
        return Scenario(agent, session, lambda _: None, state, review_check, lambda n,p: True, state_database=ledger.path)

    turns = ()
    if kind == "consented_closure":
        turns = (Turn("Yes, please withdraw my draft for that refunded charge.", partial(score, 2)),)
    return Task(kind, opening, setup, partial(score, 1), turns)
