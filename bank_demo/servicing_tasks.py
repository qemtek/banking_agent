"""Generated payment, account, deposit, loan, ATM and card servicing tasks."""
import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import date, timedelta
from functools import partial
from uuid import uuid4

from evaluation import Check, Scenario, Task, Turn, communication
from harness import Tool

from . import Session, build_agent
from .ledger import gbp
from .servicing_oracle import expected_service
from .servicing_rules import AMOUNT_COMMANDS, COMMANDS, apply_command, finance_quote
from .servicing_worlds import SERVICING_VARIANTS, servicing_messages, servicing_world

FINDINGS = {
    "payment_instruction": "shortfall, covered, skipped, released, scheduled, amended, cancelled, requested",
    "cheque_service": "stopped, cleared, requested",
    "account_lifecycle": "opened, closed, opened_and_closed, blocked, ineligible",
    "term_deposit": "quoted, booked, insufficient, renewal_changed, closed, below_minimum",
    "loan_service": "due, illustrated, paid, pending, settled, requested, over_cap",
    "atm_fee": "credited, reconciled",
    "dispute_credit_limit": "approved_and_disputed, referred_and_disputed, protected_and_disputed, disputed_only",
}

NOTICE_AMOUNTS = {
    "payment_instruction": "Use the payment amount for a skip, schedule or amendment, the shortage for coverage, zero for a series cancellation or request.",
    "cheque_service": "Use the instrument amount, zero for a book order.",
    "account_lifecycle": "Use the residual swept on a viable closure and zero for blocked closures and a standalone opening.",
    "term_deposit": "Use projected maturity for booking or quote, payout for early closure, zero for changing renewal.",
    "loan_service": "Use the instalment, illustration or total settlement quote according to the request.",
    "atm_fee": "Use the outstanding overcharge before any new credit. Include charged and correct fees in the explanation.",
    "dispute_credit_limit": "For an estimate use the requested limit. For an outcome use the approved or pending requested limit, or the retained limit on a dispute-only outcome.",
}

COMMAND_HELP = {
    "skip_payment": "Skip one still-scheduled execution.",
    "cancel_series": "Cancel a standing instruction and all still-scheduled executions.",
    "amend_series": "Change the series and future scheduled executions to the new amount.",
    "schedule_payment": "Schedule a single new payment to the payee on its specified date.",
    "request_cancellation": "Raise a pending cancellation ticket for a request-only mandate.",
    "stop_cheque": "Stop only an issued cheque on its verified account.",
    "order_cheque_book": "Request one cheque book to the verified destination of this account.",
    "open_savings": "Open the selected savings product if a current account meets its live tenure rule.",
    "close_current": "Close an eligible current account and remit its residual balance to its confirmed destination.",
    "book_deposit": "Book a quoted offer and debit the first contribution from its linked current account.",
    "change_renewal": "Change an active deposit maturity instruction to pay out.",
    "redeem_deposit": "Close a deposit early at its current authorised payout.",
    "pay_instalment": "Pay one currently due instalment from the linked account.",
    "settle_loan": "Debit the settlement quote and close a direct-settlement loan.",
    "request_settlement": "Open a pending loan settlement ticket without debiting the customer.",
    "credit_atm_fees": "Post the reconciled outstanding credit to the audited account.",
    "request_limit": "Submit the specified new limit for this card to the credit service.",
    "freeze_card": "Protect a reported lost or stolen card.",
    "file_dispute": "File a dispute on the identified charge.",
}


def notice_references(world):
    """Accept the named objects of a compound request when attaching its notice."""
    refs = {world["subject"]}
    if world["family"] == "account_lifecycle" and world["flow"] == "compound":
        refs.update(ref for op, ref in world["scope"] if op in ("open_savings","close_current"))
    if world["family"] == "dispute_credit_limit":
        refs.update(key for key,row in world["records"].items()
                    if row["owner"] == "alice" and row["kind"] == "card" and row["label"] == "Travel")
    return refs


def permitted_service_notices(world, outcome):
    refs = {"unresolved"} if outcome.finding == "clarify" else notice_references(world)
    notices = [{"reference":ref, "finding":outcome.finding, "amount_pence":outcome.amount} for ref in sorted(refs)]
    # Failed eligibility blocks only the explicitly conditional closure. A closure notice
    # addresses that account; the product's own finding is still ineligible.
    if (outcome.finding == "ineligible" and not outcome.operations and outcome.amount == 0
            and world["policy"].get("close_requires_savings")):
        notices.extend({"reference":ref, "finding":"blocked", "amount_pence":0}
                       for op,ref in world["scope"] if op == "close_current")
    return notices


class ServicingDesk:
    def __init__(self, directory, case):
        self.case, self.w = case, case["world"]
        self.path = directory / f"ledger-{uuid4().hex}.sqlite"
        with self.db() as db:
            db.executescript("""
                CREATE TABLE records(ref TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE events(seq INTEGER PRIMARY KEY, turn INTEGER, kind TEXT, ref TEXT, value TEXT);
                CREATE TABLE clock(turn INTEGER);
                INSERT INTO clock VALUES (0);
            """)
            db.executemany("INSERT INTO records VALUES (?,?)", ((ref, json.dumps(row)) for ref,row in self.w["records"].items()))

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            yield db

    @staticmethod
    def records(db):
        return {ref: json.loads(raw) for ref, raw in db.execute("SELECT ref,body FROM records")}

    @staticmethod
    def event(db, kind, ref="", value=None):
        db.execute("INSERT INTO events(turn,kind,ref,value) SELECT turn,?,?,? FROM clock", (kind, ref, json.dumps(value)))

    @staticmethod
    def turn(db):
        return db.execute("SELECT turn FROM clock").fetchone()[0]

    def advance(self, turn):
        with self.db() as db:
            prior = self.turn(db)
            db.execute("UPDATE clock SET turn=?", (turn,))
            if turn == 2 and prior < 2:
                rows = self.records(db)
                for ref, changes in self.w["later"].items():
                    rows[ref].update(changes)
                    db.execute("UPDATE records SET body=? WHERE ref=?", (json.dumps(rows[ref]), ref))

    def directory(self, session):
        with self.db() as db:
            visible = {ref: r for ref, r in self.records(db).items()
                       if r["owner"] == session.customer_id and r["kind"] != "withdrawal"}
            full = self.case["difficulty"] == "easy"
            self.event(db, "directory", value=list(visible) if full else [])
            return {"records": [{"reference": ref, **({k:v for k,v in r.items() if k != "owner"} if full else
                                 {"kind": r["kind"], "label": r["label"]})} for ref, r in visible.items()]}

    def detail(self, session, reference):
        with self.db() as db:
            row = self.records(db).get(reference, {})
            if row.get("owner") != session.customer_id:
                self.event(db, "denied", reference, "private or unknown record")
                return {"error": "Record unavailable"}
            self.event(db, "read", reference)
            return {"reference": reference, **{k:v for k,v in row.items() if k != "owner"}}

    def policy(self, session):
        with self.db() as db:
            self.event(db, "policy")
        if "policy_editions" in self.w:
            return {"as_of": self.w["as_of"], "documents": self.w["policy_editions"],
                    "selection": "Use the most recent edition effective on or before the service date."}
        policy = {k:v for k,v in self.w["policy"].items() if k not in ("settlement_cap", "minimum_payout", "requested_limit")}
        terms = [{"edition": self.w["edition"], "effective_from": self.w["as_of"], "terms": policy}]
        if self.case["difficulty"] == "hard":
            prior = dict(policy, rule="Previous edition. Use manual processing for all monetary changes.")
            if "free" in prior:
                prior["free"] += 1
            terms.insert(0, {"edition": "archived", "through": str(date.fromisoformat(self.w["as_of"])-timedelta(days=1)), "terms": prior})
        return {"as_of": self.w["as_of"], "documents": terms}

    def quote(self, session, reference):
        with self.db() as db:
            row = self.records(db).get(reference, {})
            if row.get("owner") != session.customer_id:
                self.event(db, "denied", reference, "unavailable quote object")
                return {"error": "Record unavailable"}
            try:
                amount = finance_quote(row)
            except ValueError as e:
                return {"error": str(e)}
            self.event(db, "quote", reference, amount)
            if self.case["variant"] == "settlement_fee_estimate":
                return {"reference": reference, "as_of": self.w["as_of"], "pence": amount,
                        "basis": "Gross early settlement before any policy-based fee relief."}
            return {"reference": reference, "as_of": self.w["as_of"], "pence": amount,
                    "basis": "maturity" if row["kind"] == "offer" else "early settlement"}

    def withdrawals(self, session, account_reference):
        with self.db() as db:
            rows = self.records(db)
            if rows.get(account_reference, {}).get("owner") != session.customer_id:
                self.event(db, "denied", account_reference, "unavailable account")
                return {"error": "Account unavailable"}
            self.event(db, "atm_statement", account_reference)
            return {"entries": [{"reference": k, **{f:v for f,v in r.items() if f != "owner"}} for k,r in rows.items()
                                if r["kind"] == "withdrawal" and r["owner"] == session.customer_id and r["account"] == account_reference]}

    def breakdown(self, session, account_reference, charged_pence, due_pence, withdrawal_references):
        with self.db() as db:
            if self.records(db).get(account_reference, {}).get("owner") != session.customer_id:
                self.event(db, "denied", account_reference, "unavailable reconciliation account")
                return {"error": "Account unavailable"}
            try:
                charged, due = int(charged_pence), int(due_pence)
            except ValueError:
                return {"error": "Integer pence required"}
            refs = sorted(item.strip() for item in withdrawal_references.split(",") if item.strip())
            value = {"account":account_reference, "charged":charged, "due":due, "withdrawals":refs}
            self.event(db, "breakdown", account_reference, value)
            return {"sent_breakdown": value}

    def notice(self, session, reference, finding, amount_pence):
        with self.db() as db:
            if reference != "unresolved" and self.records(db).get(reference, {}).get("owner") != session.customer_id:
                self.event(db, "denied", reference, "unavailable notice object")
                return {"error": "Record unavailable"}
            try:
                amount = int(amount_pence)
            except ValueError:
                return {"error": "Integer pence required"}
            item = {"reference": reference, "finding": finding, "amount_pence": amount}
            self.event(db, "notice", reference, item)
            return {"sent_notice": item}

    def current_permissions(self, db, session):
        if session.customer_id != "alice" or self.turn(db) < 2:
            return []
        return self.w["scope"]

    def permissions(self, session):
        with self.db() as db:
            permissions = self.current_permissions(db, session)
            self.event(db, "permissions_read")
            return {"confirmed_actions": [{"operation": op, "reference": ref} for op,ref in permissions],
                    "scope": "Only the listed operations are app-confirmed. Customer conditions and bank policy still apply."}

    def write(self, op, session, reference, amount_pence="0"):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = self.records(db)
            try:
                if rows.get(reference, {}).get("owner") != session.customer_id:
                    raise ValueError("Record unavailable")
                if [op, reference] not in self.current_permissions(db, session):
                    raise ValueError("No current app confirmation for this action and target")
                notice_refs = {"unresolved"} if self.w["flow"] == "clarify" else notice_references(self.w)
                finding = "clarify" if self.w["flow"] == "clarify" else "estimate"
                refs = db.execute("SELECT ref FROM events WHERE turn=1 AND kind='notice' "
                                  "AND json_extract(value,'$.finding')=?", (finding,))
                if not any(ref in notice_refs for (ref,) in refs):
                    raise ValueError("Customer has not received the requested initial notice")
                if db.execute("SELECT 1 FROM events WHERE kind='write' AND ref=? AND json_extract(value,'$.operation')=?",
                              (reference,op)).fetchone():
                    raise ValueError("Operation already completed")
                amount = int(amount_pence)
                authorised = {"request_limit":self.w["policy"].get("requested_limit"), "amend_series":self.w["amount"]+700,
                              "schedule_payment":self.w["amount"]}.get(op)
                if authorised is not None and amount != authorised:
                    raise ValueError("Amount outside customer instruction")
                if op in ("book_deposit","redeem_deposit","settle_loan","request_settlement"):
                    current_quote = finance_quote(rows[reference])
                    if not db.execute("SELECT 1 FROM events WHERE kind='quote' AND ref=? AND value=?", (reference,str(current_quote))).fetchone():
                        raise ValueError("Current quote required")
                apply_command(rows, self.w, op, reference, amount)
            except (KeyError, ValueError) as error:
                self.event(db, "denied", reference, {"operation": op, "error": str(error)})
                return {"error": str(error)}
            observed = self.records(db)
            for key, row in rows.items():
                if row != observed.get(key):
                    db.execute("INSERT OR REPLACE INTO records VALUES (?,?)", (key, json.dumps(row)))
            count = db.execute("SELECT count(*) FROM events WHERE kind='write'").fetchone()[0]
            receipt = {"receipt": f"RC-{self.w['new_ref'][4:]}-{count+1}", "operation":op, "reference":reference}
            if self.w["new_ref"] in rows and self.w["new_ref"] not in observed:
                receipt["created_reference"] = self.w["new_ref"]
            self.event(db, "write", reference, receipt)
            return receipt

    def tools(self):
        family = self.case["family"]
        tools = {"list_servicing_records": Tool("List the signed-in customer's record references and names.", self.directory),
                 "read_servicing_record": Tool("Read one current servicing record including linked accounts, statuses and amounts in pence.", self.detail, parameters=("reference",)),
                 "read_servicing_permissions": Tool("Read this signed-in customer's currently app-confirmed operations and targets. "
                      "An empty list grants no write permission. Independently check the instruction, conditions and bank records.", self.permissions),
                 "read_servicing_policy": Tool("Read servicing rules and tariff editions with their effective dates.", self.policy),
                 "send_servicing_notice": Tool("Deliver a structured notice to the customer. Use the subject reference, or unresolved when "
                      "clarifying an ambiguous target. First-turn preview of a change uses estimate or clarify (zero pence for clarify). "
                      "A conclusive no-change finding may be sent as soon as established. On a compound request either named "
                      "action target identifies the notice. Final finding choices: " + FINDINGS[family] + ". " + NOTICE_AMOUNTS[family], self.notice,
                      parameters=("reference","finding","amount_pence"))}
        for op in COMMANDS[family]:
            params = ("reference","amount_pence") if op in AMOUNT_COMMANDS else ("reference",)
            tools[op] = Tool(COMMAND_HELP[op], partial(self.write, op), parameters=params)
        if family in ("term_deposit","loan_service"):
            tools["get_servicing_quote"] = Tool("Read a current offer maturity or an existing loan/deposit early-exit quote in pence.",
                                               self.quote, parameters=("reference",))
        if family == "atm_fee":
            tools["send_atm_breakdown"] = Tool("Deliver the requested fee calculation from the statement. Supply net bank "
                "charges before adjustments, fees due, and a comma-separated list of all in-period withdrawal references. "
                "Send the breakdown with the initial audit.", self.breakdown,
                parameters=("account_reference", "charged_pence", "due_pence", "withdrawal_references"))
            tools["read_atm_statement"] = Tool("Read dated withdrawal and fee rows for one owned account.", self.withdrawals,
                                              parameters=("account_reference",))
        return tools

    def state(self, turn):
        w = self.w
        expected = expected_service(self.case, turn)
        with self.db() as db:
            actual = self.records(db)
            history = [(t,k,r,json.loads(v)) for t,k,r,v in db.execute("SELECT turn,kind,ref,value FROM events ORDER BY seq")]
        local = [event for event in history if event[0] == turn]
        if turn == 2 and "clarification_decision" in self.case:
            preview = {"reference": w["subject"], "finding": "estimate", "amount_pence": expected.amount}
            boundary = next((i for i,(_,kind,_,v) in enumerate(local) if kind == "write" or
                             (kind == "notice" and v != preview)), len(local))
            # An accurate preview before the final action is allowed. A final outcome notice is still required.
            local = [e for i,e in enumerate(local) if i >= boundary or e[1] != "notice"]
        writes = [[v["operation"],r] for _,k,r,v in history if k == "write"]
        notices = [v for _,k,_,v in local if k == "notice"]
        denied = [v for _,k,_,v in history if k == "denied"]
        desired = {"reference": "unresolved" if expected.finding == "clarify" else w["subject"],
                   "finding": expected.finding, "amount_pence": expected.amount}
        permitted_notices = permitted_service_notices(w, expected)
        # A customer can receive a conclusive no-change finding before returning to the app.
        # Preserve the requirement for confirmation on every branch that needs a write.
        early = None
        if w["scope"] and w["flow"] != "clarify":
            initial = expected_service(self.case, 1, current_outcome=True)
            if not initial.operations:
                early = permitted_service_notices(w, initial)
                if turn == 1:
                    permitted_notices.extend(early)
        already_delivered = (turn == 2 and not w["later"] and early and desired in early and
                             any(t == 1 and k == "notice" and v in early for t,k,_,v in history))
        valid_notices = bool(notices or already_delivered) and all(n in permitted_notices for n in notices)
        ordered = writes == expected.operations
        if self.case["variant"] == "independent_cards" or self.case["variant"] == "either_order":
            ordered = sorted(writes) == sorted(expected.operations)
        first_notice = next((i for i,e in enumerate(local) if e[1] == "notice"), len(local))
        delivered_last = all(i < first_notice for i,e in enumerate(local) if e[1] == "write")
        current_cutoff = next((i for i,e in enumerate(history) if e[0] == turn and e[1] in ("notice","write")),len(history))
        available = history[:current_cutoff]
        reads = {r for _,k,r,_ in available if k == "read"}
        for _,kind,_,values in available:
            if kind == "directory":
                reads.update(values)
        before = history[:next((i for i,e in enumerate(history) if e[1] in ("notice","write")),len(history))]
        first_read_kinds = {k for _,k,_,_ in before}
        required = expected.evidence
        if turn == 1 and early and any(n in early for n in notices):
            required = initial.evidence
        evidence = required <= reads and (w["flow"] == "clarify" or "policy" in first_read_kinds)
        if any(op == "open_savings" for op,_ in w["scope"]):
            subject = w["records"][w["subject"]]
            accounts = {ref:r for ref,r in w["records"].items() if r["kind"] == "account" and r["owner"]=="alice"}
            eligible = {ref for ref,r in accounts.items() if r["status"]=="open" and r["age_days"]>=subject["minimum_tenure_days"]}
            evidence &= bool(eligible & reads) if eligible else accounts.keys() <= reads
        breakdown_ok = True
        if self.case["family"] == "atm_fee":
            p = w["policy"]
            entries = {ref:r for ref,r in w["records"].items() if r["kind"]=="withdrawal" and r["account"]==w["subject"]
                       and r["day"][:7]==p["period"]}
            charged = sum(r["bank_fee"]-r["reversal"] for r in entries.values())
            due = max(0, sum(r["network"]=="partner" for r in entries.values())-p["free"])*p["fee"]
            wanted = {"account":w["subject"],"charged":charged,"due":due,"withdrawals":sorted(entries)}
            reported = [v for t,k,_,v in history if k=="breakdown" and t==1]
            breakdown_ok = bool(reported) and all(v==wanted for v in reported)
            evidence &= any(k == "atm_statement" and r == w["subject"] for _,k,r,_ in before)
        fee_estimate = self.case["variant"] == "settlement_fee_estimate"
        need_quote = (fee_estimate or w["records"][w["subject"]]["kind"] == "offer" or
                      bool({"redeem_deposit","settle_loan","request_settlement"} & {op for op,_ in w["scope"]}))
        if need_quote:
            # The saved quote must match the terms in force at the notice or mutation.
            latest = next((e[3] for e in reversed(available) if e[1] == "quote" and e[2] == w["subject"]),None)
            quote_amount = finance_quote(w["records"][w["subject"]]) if fee_estimate else expected.amount
            evidence &= any(k == "quote" and r == w["subject"] for _,k,r,_ in before) and latest == quote_amount
        if turn == 2 and w["later"]:
            evidence &= any(t == turn and k == "read" and r == w["subject"] for t,k,r,_ in available)
        # The fee-enquiry customer gives the account on return and requests verification
        # before the change. Its opening general fee quote needs no instrument lookup.
        if w["flow"] == "clarify" and turn == 1 and "fee_enquiry" not in self.case:
            evidence &= "directory" in first_read_kinds and bool(reads)
        mismatches = [r for r in sorted(actual.keys() | expected.rows.keys()) if actual.get(r) != expected.rows.get(r)]
        return [Check("Record ownership, balances, lifecycle and linked objects", not mismatches, str(mismatches), "bank_writes"),
                Check("Only the authorized changes committed in a valid order", ordered, f"expected {expected.operations}, got {writes}", "bank_writes"),
                Check("No forbidden or premature operation attempted", not denied, str(denied), "access"),
                Check("Read the applicable evidence and required current quote", bool(evidence), str(sorted(reads)), "evidence"),
                Check("Delivered the correct scoped notice after any action", breakdown_ok and valid_notices and delivered_last,
                      f"permitted {permitted_notices}, delivered {notices}, previously delivered {already_delivered}", "outcome_delivery")]


def clarification_policy_timing(case, result, history):
    # Record facts can identify the customer’s item. A quote or an attempted change requires policy.
    w = case["world"]
    policy_seen = False
    premature = []
    for turn, events in enumerate(history, 1):
        app = {}
        for event in events:
            data = event["payload"]
            name = event["component"]
            if event["event_type"] == "tool.completed":
                value = data["value"]
                if name == "read_servicing_policy" and value.get("documents"):
                    policy_seen = True
                elif name == "read_servicing_permissions":
                    app = value.get("app_decision") or {}
            if event["event_type"] != "tool.requested" or policy_seen or turn != len(history):
                continue
            name, args = data["name"], data["arguments"]
            if name in COMMAND_HELP:
                premature.append(name)
            if name != "send_servicing_notice":
                continue
            if isinstance(args, dict):
                if (args.get("reference") == "unresolved" and args.get("finding") == "clarify"
                        and str(args.get("amount_pence")) == "0"):
                    continue
                if (args.get("finding") in ("pending", "withdrawn") and args.get("finding") == app.get("status")
                        and args.get("reference") == app.get("reference") == w["subject"]
                        and app.get("operation") == w["scope"][0][0]
                        and str(args.get("amount_pence")) == str(app.get("amount_pence"))):
                    continue
            premature.append(str(args))
    return [Check("Read effective policy before quoting terms or requesting a change", not premature,
                  f"Premature requests: {premature}", "evidence")]


def render_service_reply(result, events):
    parts = []
    for event in events:
        if event["event_type"] != "tool.completed":
            continue
        value = event["payload"]["value"]
        if value.get("sent_notice"):
            n = value["sent_notice"]
            parts.append(f"Bank notice — {n['reference']} · {n['finding'].replace('_',' ')} · £{gbp(n['amount_pence'])}")
        if value.get("sent_breakdown"):
            b = value["sent_breakdown"]
            parts.append(f"ATM reconciliation {b['account']} · charged £{gbp(b['charged'])} · due £{gbp(b['due'])}"
                         f" · withdrawals {', '.join(b['withdrawals'])}")
        if value.get("receipt"):
            new = f" · {value['created_reference']}" if "created_reference" in value else ""
            parts.append(f"Bank receipt {value['receipt']} · {value['operation'].replace('_',' ')} · {value['reference']}{new}")
    return replace(result, reply="\n".join([*parts, result.reply])) if parts else result


def servicing_task(case, factory, *, desk_class=ServicingDesk):
    messages = case["messages"]
    w = case["world"]

    def setup(directory):
        desk = desk_class(directory, case)
        agent = build_agent(directory, model=factory(), case_tools=desk.tools())
        # These worlds model multiple accounts with their own balances and histories.
        for name in ("get_available_balance","get_recent_transactions"):
            del agent.tools[name]
        agent.instructions += ("\nWork from the servicing records and the effective policy. Use integer GBP pence. "
                               "Present estimates and results using send_servicing_notice and explain them in chat. "
                               "Use get_servicing_quote for deposits and early loan settlement. An enquiry has one final finding. "
                               "For a change the first notice is an estimate, or clarify for a disputed or ambiguous target. "
                               "Use the named product, instrument, charge or audited account as the notice reference. "
                               "App confirmation takes effect on the customer's return and is available from read_servicing_permissions. "
                               "An empty list grants no write permission. Recheck changed records and respect actual signed scope. Use the available operation, then deliver its outcome. "
                               "Supply references from retrieved records. " + NOTICE_AMOUNTS[case["family"]])
        if case["variant"] == "settlement_fee_estimate":
            agent.instructions += " For a settlement estimate enquiry, send the net amount using the estimate finding."
        return Scenario(agent, Session("alice"), desk.advance, desk.state, state_database=desk.path)

    def score(n, result, events):
        e = expected_service(case,n)
        explanation = f"{e.finding.replace('_',' ')}. £{gbp(e.amount)}. {e.details}"
        if w["scope"] and w["flow"] != "clarify":
            initial = expected_service(case, 1, current_outcome=True)
            if not initial.operations:
                if n == 1:
                    explanation += f" The bank may instead report the established {initial.finding} finding now."
                elif not w["later"]:
                    explanation += " An acknowledgement suffices if the correct result was already delivered."
        if w["flow"] == "clarify":
            explanation += (" An identification question may cite retrieved record facts. Any service terms or fee "
                            "given in chat need evidence available before the reply. A matching app decision "
                            "can support a report of what the app displayed.")
        return [Check("Completed the customer turn", result.stop_reason == "final_reply", result.stop_reason, "delivery"),
                communication("Explains the actual outcome and conditions", None, result.reply,
                              explanation + " Address the customer's request and any blockers.")]

    return Task(case["variant"], messages[0], setup, partial(score,1),
                tuple(Turn(m,partial(score,i)) for i,m in enumerate(messages[1:],2)), render_reply=render_service_reply,
                score_history=partial(clarification_policy_timing, case) if w["flow"] == "clarify" else None)
