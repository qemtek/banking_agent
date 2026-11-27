"""Reconcile a reopened dispute against the merchant postings for its purchase."""
import random
import sqlite3
from contextlib import closing
from dataclasses import replace
from uuid import uuid4

from evaluation import Check, Scenario, Task, Turn, communication
from harness import Tool
from . import Session, build_agent
from .ledger import gbp


FAMILY = "refund"
OUTCOMES = ("merchant_refund_pending", "merchant_refund_full", "merchant_refund_partial", "merchant_refund_unrelated")
DEVELOPMENT_SOURCE = "fos:DRN-6050993"
POLICY = """
For a returned purchase, use the dispute file and the latest account postings. Reopen an
evidence-expired dispute after verified return evidence is received and reinstate its temporary
bank credit. A merchant's submission or pending credit does not settle the purchase. Offset
reinstated relief only by posted merchant money linked to the disputed purchase, up to the
relief still outstanding. Leave a partially or provisionally covered dispute open. Resolve it
once the entire purchase is permanently refunded. Do not change other purchases or remove the
merchant's money. Send a current dispute statement when the customer asks for one.
"""

OPENINGS = {
    "cooperative": "The credit for my returned purchase disappeared. I've sent the return evidence. Could you sort it out and send me an updated statement in the app?",
    "uncertain": "I'm confused. I sent my return confirmation, but the credit disappeared. Can you check it and send me a statement in the app?",
    "pressuring": "I sent the return confirmation and my credit's gone. Please get this sorted today and send the statement to my app.",
    "mistaken": "I thought the credit was my shop refund. I've sent my return confirmation. Can you sort it out and send the statement to my app?",
    "misleading": "The shop says it's all settled, but the credit vanished. I sent my return evidence. Can you sort this out and send my statement to the app?",
}
RETURN = "The shop says it has processed my refund. Can you check what's actually been paid back, sort out my dispute and send me the updated statement in the app?"


def posting(reference, purchase, issuer, status, pence):
    return dict(reference=reference, purchase=purchase, issuer=issuer, status=status, pence=pence)


def credit_world(seed, variant, difficulty):
    rng = random.Random(f"provisional-credit/{seed}")
    ids = rng.sample(range(100000, 999999), 12)
    target, other = (f"TX-{i}" for i in ids[:2])
    amount = rng.randrange(3001, 48000)
    initial = [posting(f"B-{ids[2]}", target, "bank", "posted", amount),
               posting(f"B-{ids[3]}", target, "bank", "posted", -amount)]
    if difficulty in ("medium", "hard"):
        initial.append(posting(f"M-{ids[4]}", other, "merchant", "posted", amount))
    if difficulty == "hard":
        initial.append(posting(f"M-{ids[5]}", target, "merchant", "pending", amount))
    incoming = posting(f"M-{ids[6]}", other if variant == OUTCOMES[3] else target, "merchant",
                       "pending" if variant == OUTCOMES[0] else "posted",
                       rng.randrange(1, amount) if variant == OUTCOMES[2] else amount)
    return dict(purchase=target, amount=amount, case_ref=f"DIS-{ids[7]}", initial=initial, incoming=incoming,
                reinstatement=f"B-{ids[8]}", offset=f"B-{ids[9]}")


def credit_messages(behaviour):
    return [OPENINGS[behaviour], RETURN]


def expected(w, turn):
    rows = list(w["initial"])
    rows.append(posting(w["reinstatement"], w["purchase"], "bank", "posted", w["amount"]))
    settled = 0
    if turn == 2:
        new = w["incoming"]
        rows.append(new)
        if new["purchase"] == w["purchase"] and new["status"] == "posted":
            settled = new["pence"]
            rows.append(posting(w["offset"], w["purchase"], "bank", "posted", -settled))
    return rows, (settled, w["amount"] - settled, "resolved" if settled == w["amount"] else "open")


def credit_task(case, factory):
    w = case["world"]
    case_ref, purchase, amount = w["case_ref"], w["purchase"], w["amount"]
    required_reads = {"read_dispute_file", "read_credit_postings"}

    def setup(directory):
        database = directory / f"ledger-{uuid4().hex}.sqlite"

        def insert(db, row):
            db.execute("INSERT INTO postings VALUES(:reference,:purchase,:issuer,:status,:pence)", row)

        with closing(sqlite3.connect(database)) as db, db:
            db.executescript("""
                CREATE TABLE dispute(turn INTEGER, status TEXT);
                INSERT INTO dispute VALUES(0,'closed_evidence_expired');
                CREATE TABLE postings(reference TEXT PRIMARY KEY, purchase TEXT, issuer TEXT, status TEXT, pence INTEGER);
                CREATE TABLE notices(seq INTEGER PRIMARY KEY, turn INTEGER, permanent INTEGER, temporary INTEGER, status TEXT);
            """)
            for row in w["initial"]:
                insert(db, row)

        def advance(turn):
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                current, = db.execute("SELECT turn FROM dispute").fetchone()
                if turn > current:
                    if turn == 2:
                        insert(db, w["incoming"])
                    db.execute("UPDATE dispute SET turn=?", (turn,))

        def read_file(session):
            with closing(sqlite3.connect(database)) as db:
                status, = db.execute("SELECT status FROM dispute").fetchone()
            return dict(case_ref=case_ref, purchase=purchase, disputed_pence=amount, status=status,
                        return_evidence="verified", previous_closure="evidence deadline expired")

        def read_postings(session):
            with closing(sqlite3.connect(database)) as db:
                db.row_factory = sqlite3.Row
                return {"entries": [dict(row) for row in db.execute("SELECT * FROM postings ORDER BY rowid")]}

        def totals(db):
            values = dict(db.execute("SELECT issuer, SUM(pence) FROM postings WHERE purchase=? AND status='posted' GROUP BY issuer",
                                     (purchase,)))
            return values.get("merchant", 0), values.get("bank", 0)

        def reopen(session, dispute_ref):
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if dispute_ref != case_ref:
                    return {"error": "Unknown dispute."}
                status, = db.execute("SELECT status FROM dispute").fetchone()
                if status == "resolved":
                    return {"error": "Dispute already settled."}
                if status == "closed_evidence_expired":
                    insert(db, posting(w["reinstatement"], purchase, "bank", "posted", amount))
                    db.execute("UPDATE dispute SET status='open'")
                return {"reference": w["reinstatement"]}

        def reconcile(session, dispute_ref, merchant_ref, pence):
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                merchant = db.execute("SELECT pence FROM postings WHERE reference=? AND purchase=? AND issuer='merchant' AND status='posted'",
                                      (merchant_ref, purchase)).fetchone()
                status, = db.execute("SELECT status FROM dispute").fetchone()
                if dispute_ref != case_ref or status == "closed_evidence_expired" or not merchant:
                    return {"error": "No eligible settlement for this dispute."}
                permanent, temporary = totals(db)
                existing = db.execute("SELECT pence FROM postings WHERE reference=?", (w["offset"],)).fetchone()
                adjustment = -existing[0] if existing else min(merchant[0], temporary)
                if adjustment <= 0 or pence.lstrip("0") != str(adjustment):
                    return {"error": "Amount differs from the eligible offset in pence."}
                if not existing:
                    insert(db, posting(w["offset"], purchase, "bank", "posted", -adjustment))
                    if permanent >= amount:
                        db.execute("UPDATE dispute SET status='resolved'")
                return {"reference": w["offset"]}

        def send_statement(session, dispute_ref):
            if dispute_ref != case_ref:
                return {"error": "Unknown dispute."}
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                permanent, temporary = totals(db)
                turn, status = db.execute("SELECT turn,status FROM dispute").fetchone()
                db.execute("INSERT INTO notices(turn,permanent,temporary,status) VALUES(?,?,?,?)",
                           (turn, permanent, temporary, status))
            return {"customer_text": f"Dispute {case_ref} ({status}). Merchant refund £{gbp(permanent)}. Temporary bank credit £{gbp(temporary)}."}

        tools = {
            "read_dispute_file": Tool("Read the customer's dispute, purchase and return evidence.", read_file),
            "read_credit_postings": Tool("Read posted and pending account credits, issuers and purchase links in pence.", read_postings),
            "reopen_with_relief": Tool("Reopen a verified returned-purchase claim and reinstate temporary relief once.", reopen, parameters=("dispute_ref",)),
            "offset_bank_credit": Tool("Offset temporary relief against a posted merchant refund. Supply integer pence as digits.", reconcile,
                                       parameters=("dispute_ref", "merchant_ref", "pence")),
            "send_dispute_statement": Tool("Send the current dispute status and credit breakdown to the customer's app.", send_statement,
                                           parameters=("dispute_ref",)),
        }

        def score_state(turn):
            wanted_rows, breakdown = expected(w, turn)
            with closing(sqlite3.connect(database)) as db:
                db.row_factory = sqlite3.Row
                rows = [dict(r) for r in db.execute("SELECT * FROM postings ORDER BY reference")]
                status = db.execute("SELECT status FROM dispute").fetchone()[0]
                notices = [tuple(r) for r in db.execute("SELECT permanent,temporary,status FROM notices WHERE turn=? ORDER BY seq", (turn,))]
            return [Check("Preserves all credits and offsets only settled merchant money", rows == sorted(wanted_rows, key=lambda r: r["reference"]),
                          str(rows), "bank_writes"),
                    Check("Saves the correct dispute status", status == breakdown[2], status, "bank_writes"),
                    Check("Delivers an accurate current breakdown to the app", bool(notices) and all(n == breakdown for n in notices),
                          f"delivered={notices}, expected={breakdown}", "delivery")]

        agent = build_agent(directory, model=factory(), case_tools=tools)
        agent.instructions += "\n" + POLICY
        return Scenario(agent, Session("alice"), advance, score_state, state_database=database)

    def grade(result, history):
        turn = len(history)
        # The dispute identity and verified evidence carry forward. Merchant postings can change.
        seen = {e["component"] for events in history[:-1] for e in events
                if e["event_type"] == "tool.completed" and e["component"] == "read_dispute_file"}
        bad = []
        _, (permanent, temporary, status) = expected(w, turn)
        for e in history[-1]:
            payload = e["payload"]
            if e["event_type"] == "tool.completed":
                if e["component"] in required_reads:
                    seen.add(e["component"])
                elif e["component"] in ("reopen_with_relief", "offset_bank_credit") and "error" in payload["value"]:
                    bad.append((e["component"], payload["value"]["error"]))
            if e["event_type"] == "tool.requested" and payload["name"] in ("reopen_with_relief", "offset_bank_credit"):
                name, args = payload["name"], payload["arguments"]
                supported = seen == required_reads and args.get("dispute_ref") == case_ref
                if name == "offset_bank_credit":
                    supported = (supported and permanent > 0 and args.get("merchant_ref") == w["incoming"]["reference"]
                                 and isinstance(args.get("pence"), str) and args["pence"].lstrip("0") == str(permanent))
                if not supported:
                    bad.append((name, args))
        return [Check("Links the dispute to current account postings", seen == required_reads, str(sorted(seen)), "evidence"),
                Check("All attempted adjustments were supported at the time", not bad, str(bad), "unsupported_actions"),
                communication("Explains which credit is permanent and what remains provisional", None, result.reply,
                              f"Merchant £{gbp(permanent)}, temporary bank relief £{gbp(temporary)}, dispute {status}.")]

    def render(result, events):
        delivered = [e["payload"]["value"]["customer_text"] for e in events if e["event_type"] == "tool.completed"
                     and e["component"] == "send_dispute_statement" and "customer_text" in e["payload"]["value"]]
        return replace(result, reply="\n\n".join([*delivered, result.reply])) if delivered else result

    first, second = case["messages"]
    def completed(result, events):
        return [Check("Completes the customer turn", result.stop_reason == "final_reply", result.stop_reason, "delivery")]

    return Task(case["variant"], first, setup, completed, (Turn(second, completed),),
                render_reply=render, score_history=grade)
