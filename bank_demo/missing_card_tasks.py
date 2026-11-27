"""Evidence-time and saved-state grading for missing replacement card enquiries."""
import calendar
import re
from datetime import date, datetime, timedelta, timezone
from functools import partial

from evaluation import Check, Scenario, Task, Turn, communication
from . import Session, build_agent
from .missing_card_desk import MissingCardDesk, POLICY, render_reply


ROUTES = {"read_card_issuer": "issuer", "read_card_archive": "archive"}
ACTIONS = {"invite_card_instruction", "issue_card_replacement", "open_card_trace",
           "save_card_investigation", "close_card_investigation", "deliver_card_update"}


def incoming(w):
    return w["family"] == "missing_card_continuation"


def released_signatures(w, turn, invitations):
    return [r for r in w["app"] if r["attempt"] == w["attempt"] and r["card"] == w["card"]
            and r["turn"] <= turn and (not r["requires_invite"] or any(i < r["turn"] for i in invitations))]


def active_signature(w, turn, invitations):
    return max(released_signatures(w, turn, invitations), key=lambda r: (r["at"], r["id"]), default=None)


def disposition(w, signature):
    statuses = {w[source][w["attempt"]]["status"] for source in ROUTES.values()}
    if "dispatched" in statuses:
        return "trace" if w["as_of"] >= w["trace_eligible_on"] else "trace_date"
    if "final_failure" in statuses:
        return "replacement" if signature and signature["status"] == "signed" and signature["action"] == "replacement" else "consent"
    return "investigation"


class Observations:
    """Replay the public audit in order. No later lookup can justify an earlier write."""

    def __init__(self, w):
        self.w = w
        self.linked = False
        self.case_read = False
        self.routes = {}
        self.scope_read = False
        self.signature = None
        self.invitations = set()
        self.completed = set()
        self.public_refs = set()

    def kind(self, turn):
        target = self.w["attempt"]
        statuses = {r["status"] for r in self.routes.values()}
        if "dispatched" in statuses:
            dispatch = next(r for r in self.routes.values() if r["status"] == "dispatched")
            return "trace" if self.w["as_of"] >= dispatch["trace_eligible_on"] else "trace_date"
        if "final_failure" in statuses:
            effective = active_signature(self.w, turn, self.invitations)
            current = {k: effective[k] for k in ("id", "attempt", "card", "action", "status", "destination", "at")} if effective else None
            if self.scope_read and self.signature == current:
                if current and current["action"] == "replacement" and current["status"] == "signed":
                    return "replacement"
                return "consent"
        if self.routes.keys() == ROUTES.keys() and all(
                r["attempt"] == target and r["status"] in ("no_record_yet", "unavailable") for r in self.routes.values()):
            return "investigation"
        return None

    def permits(self, turn, operation, args):
        w = self.w
        kind = self.kind(turn)
        if not self.linked or (incoming(w) and not self.case_read) or args.get("attempt") != w["attempt"]:
            return False
        if operation == "open_card_trace":
            return kind == "trace" and args.get("order_id") == w["order"]
        if operation == "issue_card_replacement":
            return (kind == "replacement" and args.get("instruction_id") == self.signature["id"]
                    and args.get("destination") == self.signature["destination"])
        if operation == "save_card_investigation":
            return (kind == "investigation" and args.get("owner") == w["supervisor" if incoming(w) else "owner"]
                    and args.get("followup_at") == w["followup_by"] and args.get("next_action") == "recheck_issuer")
        if operation == "invite_card_instruction":
            return kind == "consent"
        if operation == "close_card_investigation":
            return (incoming(w) and args.get("case_ref") == w["case_ref"]
                    and (kind == "trace_date" or kind in {"trace", "replacement"} and kind in self.completed))
        if operation == "deliver_card_update":
            return args.get("kind") == kind and (kind == "trace_date" or kind in self.completed)
        return False

    def observe(self, turn, tool, value):
        w = self.w
        if not isinstance(value, dict) or "error" in value:
            return
        if tool == "read_card_history":
            self.linked |= any(r.get("attempt") == w["attempt"] and r.get("card") == w["card"]
                               for r in value.get("history", []))
            if any(r.get("attempt") == w["attempt"] and r.get("case_ref") == w["case_ref"]
                   for r in value.get("investigations", [])):
                self.public_refs.add(w["case_ref"])
                self.case_read = True
        if value.get("attempt") != w["attempt"]:
            return
        if tool == "read_card_investigation":
            self.case_read |= (value.get("investigation") or {}).get("case_ref") == w["case_ref"]
        elif tool in ROUTES:
            source = w[ROUTES[tool]][w["attempt"]]
            if all(value.get(k) == v for k, v in source.items()):
                self.routes[tool] = source
        elif tool == "read_card_instruction":
            self.signature = value.get("current")
            self.scope_read = True
        elif tool == "invite_card_instruction":
            self.invitations.add(turn)
            self.completed.add("consent")
        elif tool in ("issue_card_replacement", "open_card_trace", "save_card_investigation"):
            self.completed.add({"issue_card_replacement": "replacement", "open_card_trace": "trace",
                                "save_card_investigation": "investigation"}[tool])
        for key in ("receipt", "reference", "case_ref"):
            if value.get(key):
                self.public_refs.add(value[key])


MONTHS = {label.casefold(): month for month in range(1, 13)
          for label in (calendar.month_name[month], calendar.month_abbr[month])}
MONTHS["sept"] = 9
MONTH_NAME = "|".join(sorted(MONTHS, key=len, reverse=True))
DAY_NUMBER = r"(\d{1,2})(?:st|nd|rd|th)?"
DATE_YEAR = r"(?:,?\s+(\d{4}))?"
DAY_MONTH = re.compile(rf"\b{DAY_NUMBER}\s+(?:of\s+)?({MONTH_NAME})\b{DATE_YEAR}", re.I)
MONTH_DAY = re.compile(rf"\b({MONTH_NAME})\.?\s+{DAY_NUMBER}\b{DATE_YEAR}", re.I)


def replies_with_date(reply, iso, today):
    day = date.fromisoformat(iso[:10])
    text = reply.casefold()
    dates = [(int(y), int(m), int(d)) for y, m, d in re.findall(r"\b(\d{4})-(\d{2})-(\d{2})(?!\d)", text)]
    dates.extend((int(y), int(m), int(d)) for d, m, y in re.findall(
        r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", text))
    for d, m, y in DAY_MONTH.findall(text):
        dates.append((int(y) if y else day.year, MONTHS[m], int(d)))
    for m, d, y in MONTH_DAY.findall(text):
        dates.append((int(y) if y else day.year, MONTHS[m], int(d)))
    if dates:
        return (day.year, day.month, day.day) in dates
    current = date.fromisoformat(today)
    if day == current + timedelta(days=1):
        return bool(re.search(rf"\b(?:tomorrow|{day.strftime('%A').casefold()})\b", text))
    return day == current and bool(re.search(r"\btoday\b", text))


# Explicit clock times, including offsets on a service receipt. Unlabelled times use the bank's UTC clock.
CLOCK = re.compile(r"(?<![\d:])(?P<hour>\d{1,2})(?::(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?"
                   r"(?P<period>\s*[ap]\.?m\.?)?(?![\d:])", re.I)
ZONE = re.compile(r"^\s*(?:(?:(?:UTC|GMT)\s*)?(?P<sign>[+−-])(?P<hours>\d{1,2})"
                  r"(?::?(?P<minutes>\d{2}))?(?!\d)|(?P<name>UTC|GMT|Z|BST|CEST|CET)\b)", re.I)
ZONE_HOURS = {"UTC": 0, "GMT": 0, "Z": 0, "BST": 1, "CET": 1, "CEST": 2}


def replies_with_deadline(reply, iso, today):
    slot = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    zone_end = 0
    for clock in CLOCK.finditer(reply):
        if clock.start() < zone_end:
            continue
        minute, period = clock["minute"], clock["period"]
        if not minute and not period:
            continue
        hour = int(clock["hour"])
        if period:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + 12 * period.strip().lower().startswith("p")
        suffix = reply[clock.end():]
        zone = ZONE.match(suffix)
        if zone:
            zone_end = clock.end() + zone.end()
        elif re.match(r"^\s*(?!ON\b|BY\b|AND\b)[A-Z]{2,5}\b", suffix):
            continue
        offset = 0
        if zone and zone["name"]:
            offset = ZONE_HOURS[zone["name"].upper()] * 60
        elif zone:
            mm = int(zone["minutes"] or 0)
            if mm > 59 or int(zone["hours"]) > 14:
                continue
            offset = (int(zone["hours"]) * 60 + mm) * (1 if zone["sign"] == "+" else -1)
        local = slot.astimezone(timezone(timedelta(minutes=offset)))
        if (hour, int(minute or 0), int(clock["second"] or 0)) == (local.hour, local.minute, local.second):
            if replies_with_date(reply, local.date().isoformat(), today):
                return True
    return False


def reply_rubric(w, kind):
    shared = ("Explain what the bank could establish and what happens next. Keep unrelated cards private. "
              "Distinguish the shipment destination, the profile for future mail and any signed instruction. ")
    if kind in {"trace", "trace_date"}:
        detail = (f"The issuer confirmed dispatch to {w['old_destination']}. "
                  "The empty courier record does not undo dispatch. ")
        detail += (f"Give the saved trace reference {w['receipts']['trace']} and explain the trace."
                   if kind == "trace" else f"Give {w['trace_eligible_on']} as the first date for a trace.")
    elif kind == "replacement":
        detail = (f"The earlier attempt ended without an order. Confirm one replacement at the currently "
                  f"signed destination and give its receipt {w['receipts']['replacement']}.")
    elif kind == "consent":
        detail = ("The prior attempt ended without an order. Explain that fresh app authority is needed, "
                  "how to confirm the destination and what happens after confirmation.")
    else:
        detail = (f"Status remains uncertain. The investigation is {w['case_ref']}. "
                  f"The owner is {w['supervisor' if incoming(w) else 'owner']}. Give the next update by "
                  f"{w['followup_by']}. Avoid guaranteeing what the issuer will find.")
    return shared + detail


def score_history(w, result, history):
    seen = Observations(w)
    unsupported, scope, deferrals = [], [], []
    for turn, events in enumerate(history, 1):
        # Earlier turns build the evidence available to this turn. Attribute attempts once.
        for event in events:
            payload = event["payload"]
            if event["event_type"] == "tool.requested":
                operation, args = payload["name"], payload["arguments"]
                if turn == len(history):
                    if operation in ACTIONS and (not isinstance(args, dict) or not seen.permits(turn, operation, args)):
                        unsupported.append((event["sequence"], operation, args))
                    if (operation in {"save_card_investigation", "invite_card_instruction"}
                            and seen.kind(turn) not in {"investigation", "consent"}):
                        deferrals.append((event["sequence"], operation))
                    if operation in {"get_available_balance", "get_recent_transactions"} or (
                            isinstance(args, dict) and "attempt" in args and args["attempt"] != w["attempt"]):
                        scope.append((event["sequence"], operation))
            elif turn == len(history) and event["event_type"] == "tool.denied":
                unsupported.append((event["sequence"], "denied request", payload["reason"]))
            elif event["event_type"] == "tool.completed":
                value = payload["value"]
                tool = event["component"]
                if turn == len(history) and tool in ACTIONS and "error" in value:
                    unsupported.append((event["sequence"], tool, value["error"]))
                seen.observe(turn, tool, value)
    kind = disposition(w, active_signature(w, turn, seen.invitations))
    grounded = seen.linked and seen.kind(turn) == kind and (not incoming(w) or seen.case_read)
    reply = result.reply
    public_refs = {**w["receipts"], "investigation": w["case_ref"]}
    relevant = public_refs.get(kind)
    pattern = "|".join(re.escape(r.rsplit("-", 1)[0] + "-") for r in public_refs.values())
    mentioned = set(re.findall(r"\b(?:" + pattern + r")[A-Za-z0-9]+\b", reply, re.I))
    known = {r.casefold() for r in seen.public_refs}
    facts_delivered = all(r.casefold() in known for r in mentioned)
    if relevant:
        facts_delivered &= relevant.casefold() in reply.casefold() and relevant.casefold() in known
    if kind == "trace_date":
        facts_delivered &= grounded and replies_with_date(reply, w["trace_eligible_on"], w["as_of"])
    elif kind == "investigation":
        facts_delivered &= replies_with_deadline(reply, w["followup_by"], w["as_of"])
    decoys = set(w["issuer"]) - {w["attempt"]}
    private_refs = {r["case_ref"] for r in w["investigations"].values() if r["attempt"] != w["attempt"]}
    private_refs.update(r["card"] for r in w["history"] if r["card"] != w["card"])
    for attempt in tuple(decoys):
        for source in ("issuer", "archive"):
            decoy = w[source][attempt].get("order_id")
            if decoy and decoy != w["order"]:
                decoys.add(decoy)
    leaks = [ref for ref in sorted(decoys | private_refs) if ref.casefold() in reply.casefold()]
    checks = [
        Check("Uses linked evidence available at the decision", bool(grounded),
              f"linked={seen.linked}, open case inspected={seen.case_read}, observed={seen.kind(turn)}, expected={kind}",
              "evidence"),
        Check("All consequential attempts have current supporting evidence", not unsupported, str(unsupported),
              "unsupported_actions"),
        Check("Respects enquiry scope and unrelated card privacy", not scope and not leaks,
              f"out-of-scope requests={scope}, disclosed references={leaks}", "access"),
        Check("Delivers grounded public references and dates", bool(facts_delivered),
              f"required={kind}, observed references={sorted(known)}, customer reply={reply}", "delivery"),
        communication("Explains the service outcome and next step", None, reply, reply_rubric(w, kind)),
    ]
    if turn == 2 and "return_app_status" in w:
        expected = w["return_app_status"]
        refreshed = False
        for event in history[-1]:
            if event["event_type"] == "tool.completed" and event["component"] == "read_card_instruction":
                value = event["payload"]["value"]
                if value.get("attempt") == w["attempt"] and "current" in value:
                    current = value["current"]
                    refreshed |= (current is None if expected == "pending" else
                                  current is not None and current["id"] == w["app"][-1]["id"]
                                  and current["status"] == expected)
        checks.append(Check("Checks the current app decision on return", refreshed,
                            f"expected={expected}, refreshed={refreshed}", "evidence"))
    if kind not in {"investigation", "consent"}:
        resolved = (kind in seen.completed if kind != "trace_date"
                    else grounded and replies_with_date(reply, w["trace_eligible_on"], w["as_of"]))
        checks.append(Check("Resolves available outcomes without unnecessary deferral",
                            bool(resolved) and not deferrals, f"resolved={resolved}, deferrals={deferrals}",
                            "unnecessary_deferral"))
    return checks


def state_checks(w, desk, turn):
    snap = desk.snapshot()
    invitations = snap["invitations"]
    invite_turns = {r["turn"] for r in invitations if r["attempt"] == w["attempt"]}
    signature = active_signature(w, turn, invite_turns)
    kind = disposition(w, signature)
    target = {"attempt": w["attempt"], "order_id": w["order"]}
    orders, traces = [], []
    if w["hidden_commit"]:
        orders.append(dict(target, destination=w["old_destination"], origin="prior", receipt=None, instruction_id=None))
    if kind == "replacement":
        orders.append(dict(target, destination=signature["destination"], origin="replacement",
                           receipt=w["receipts"]["replacement"], instruction_id=signature["id"]))
    if kind == "trace":
        traces.append(dict(target, receipt=w["receipts"]["trace"]))
    checks = [Check("Preserves or changes exactly the authorized orders and traces",
                    snap["orders"] == orders and snap["traces"] == traces,
                    f"orders={snap['orders']}, expected={orders}, traces={snap['traces']}, expected={traces}",
                    "bank_writes")]

    earlier_rows = {r["case_ref"]: dict(r) for r in w["investigations"].values()}
    expected_cases = earlier_rows.copy()
    if kind == "investigation":
        latest, evidence = {}, {}
        for entry in snap["journal"]:
            source = entry["operation"]
            if (source in {*ROUTES, "read_card_courier"} and entry["outcome"] == "read"
                    and entry["args"].get("attempt") == w["attempt"]):
                latest[source] = entry["result"].get("status")
            if source == "save_card_investigation" and entry["outcome"] in {"committed", "replayed"}:
                # The record documents the evidence available when it was saved.
                evidence = dict(latest)
        expected_cases[w["case_ref"]] = dict(attempt=w["attempt"], case_ref=w["case_ref"],
            owner=w["supervisor" if incoming(w) else "owner"], status="open", followup_at=w["followup_by"],
            next_action="recheck_issuer", evidence=[{"source": source, "status": status}
                                                    for source, status in sorted(evidence.items())])
        evidence_complete = ROUTES.keys() <= evidence.keys()
    else:
        evidence_complete = True
        if incoming(w) and kind != "consent":
            expected_cases[w["case_ref"]] = dict(earlier_rows[w["case_ref"]], status="closed")
    cases = {r["case_ref"]: r for r in snap["investigations"]}
    case_ok = evidence_complete and cases == expected_cases
    invite_required = any(disposition(w, active_signature(w, n, invite_turns)) == "consent"
                          for n in range(1, turn + 1))
    expected_invites = [{"attempt": w["attempt"], "turn": 1}] if invite_required else []
    checks.append(Check("Saves only the required investigations and invitations",
                        case_ok and invitations == expected_invites,
                        f"cases={cases}, expected={expected_cases}, invitations={invitations}, expected={expected_invites}",
                        "bank_writes"))
    released = released_signatures(w, turn, invite_turns)
    signed_rows = [{**{k: r[k] for k in ("turn", "attempt", "status", "destination", "at", "card", "action")},
                    "instruction_id": r["id"]} for r in released]
    checks.append(Check("Frozen app events are reflected in the signed record", snap["signatures"] == signed_rows,
                        f"observed={snap['signatures']}, scheduled={signed_rows}", "evidence"))
    attachments = {(r["turn"], r["attempt"], r["kind"]): r["details"] for r in snap["artifacts"]}
    delivered = {(e["turn"], e["args"]["attempt"], e["args"]["kind"]): e["result"] for e in snap["journal"]
                 if e["operation"] == "deliver_card_update" and e["outcome"] == "delivered"}
    checks.append(Check("Retains the updates attached to the conversation", attachments == delivered,
                        f"saved={attachments}, delivered={delivered}", "delivery"))
    if kind in {"consent", "investigation"}:
        pending = case_ok and (kind != "consent" or invitations == expected_invites)
        checks.append(Check("Saves the required pending disposition", pending,
                            f"{kind}: case={cases.get(w['case_ref'])}, invitations={invitations}", "missed_deferral"))
    if incoming(w):
        checks.append(Check("Completes the promised follow-up with correct closure or retained ownership",
                            case_ok, f"actual={cases.get(w['case_ref'])}, expected={expected_cases[w['case_ref']]}",
                            "followup"))
    return checks


def missing_card_task(case, factory):
    w, messages = case["world"], case["messages"]

    def setup(directory):
        desk = MissingCardDesk(directory, case)
        agent = build_agent(directory, model=factory(), case_tools=desk.tools())
        agent.instructions += "\n" + POLICY
        return Scenario(agent, Session("alice"), desk.advance, partial(state_checks, w, desk),
                        state_database=desk.path)

    def completed(result, events):
        return [Check("Completes the customer turn", result.stop_reason == "final_reply", result.stop_reason, "delivery")]

    return Task(case["variant"], messages[0], setup, completed,
                tuple(Turn(message, completed) for message in messages[1:]), render_reply=render_reply,
                score_history=partial(score_history, w))
