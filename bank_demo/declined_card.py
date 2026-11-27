"""Missing-card contacts that start with a declined purchase."""
import random
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from uuid import uuid4

from evaluation import Check, Scenario, Task, communication
from harness import Tool
from . import Session, build_agent


FAMILY = "missing_card_contact"
OUTCOMES = ("decline_destination_signed", "decline_destination_withdrawn")
DEVELOPMENT_SOURCE = "fos:DRN2384614"
# Split at the original decision, before generating any descendants.
SOURCE_SPLITS = {DEVELOPMENT_SOURCE: "development", "fos:DRN1161374": "holdout"}

POLICY = """
For a declined purchase, identify the affected transaction and establish why the card stopped
working. Follow any successor order and check its fulfilment record. A returned and disabled
successor can be replaced once. Use the latest signed app instruction for that order and its
exact destination, even when a mailing profile or prior instruction differs. If the latest
instruction was withdrawn, request a fresh confirmation and leave the order unchanged.
Explain what happened and the next step. Give the saved reference for an order or invitation.
"""

OPENINGS = {
    "cooperative": "My card was declined at the shop today. Can you help me get it working?",
    "uncertain": "My card didn't work at the till today. I'm not sure why. Can you help?",
    "pressuring": "I need to use my card. It just got declined in a shop. Can you sort it out today?",
    "mistaken": "I thought the till was broken, but my card still isn't working. Can you check today's decline?",
    "misleading": "I was told it was the shop's fault. My card got declined there today. Can you check?",
}


def decline_world(seed, variant, difficulty):
    rng = random.Random(f"declined-card/{seed}")
    ids = rng.sample(range(100000, 999999), 12)
    today = date(2026, 1, 1) + timedelta(days=rng.randrange(360))
    card, order = f"CARD-{ids[0]}", f"ORD-{ids[1]}"
    old, current = f"ADDR-{ids[2]}", f"ADDR-{ids[3]}"
    purchase = {"date": str(today), "channel": "shop", "card": card, "reason": "card_inactive"}
    purchases = [purchase]
    if difficulty != "easy":
        purchases.insert(0, {"date": str(today - timedelta(days=1)), "channel": "online",
                             "card": f"CARD-{ids[4]}", "reason": "merchant_declined"})
    decision = {"id": f"APP-{ids[5]}", "at": str(today), "destination": current,
                "status": "signed" if variant == OUTCOMES[0] else "withdrawn", "scope": "one_replacement"}
    instructions = [decision]
    if difficulty == "hard":
        instructions.insert(0, {**decision, "id": f"APP-{ids[6]}", "at": str(today - timedelta(days=9)),
                                "status": "signed", "destination": old})
    return {"as_of": str(today), "purchases": purchases, "card": card, "order": order,
            "original_destination": old, "profile_destination": old if difficulty != "easy" else current,
            "instructions": instructions, "receipt": f"REC-{ids[7]}", "invitation": f"INV-{ids[8]}"}


def decline_task(case, factory):
    w = case["world"]
    card, order = w["card"], w["order"]
    latest = max(w["instructions"], key=lambda i: i["at"])
    authorized = latest["status"] == "signed"
    operation = "order_replacement" if authorized else "request_mailing_confirmation"
    reference = w["receipt"] if authorized else w["invitation"]
    original = (order, "returned_disabled", w["original_destination"], None)
    reads = {"read_declines", "read_card_status", "read_shipment", "read_mailing_instruction"}

    def setup(directory):
        database = directory / f"ledger-{uuid4().hex}.sqlite"
        with closing(sqlite3.connect(database)) as db, db:
            db.executescript("""
                CREATE TABLE orders(id TEXT PRIMARY KEY, status TEXT, destination TEXT, authority TEXT);
                CREATE TABLE invitations(reference TEXT PRIMARY KEY, prior_order TEXT UNIQUE);
            """)
            db.execute("INSERT INTO orders VALUES(?,?,?,?)", original)

        def status(session, card_id):
            if any(p["card"] == card_id and card_id != card for p in w["purchases"]):
                return {"card": card_id, "status": "active"}
            if card_id != card:
                return {"error": "No matching retired card."}
            return {"card": card, "status": "deactivated_on_reissue", "successor_order": order}

        def shipment(session, order_id):
            if order_id != order:
                return {"error": "Unknown shipment."}
            with closing(sqlite3.connect(database)) as db:
                row = db.execute("SELECT status,destination FROM orders WHERE id=?", (order_id,)).fetchone()
            return {"order": order_id, "status": row[0], "destination": row[1]}

        def mailing(session, order_id):
            if order_id != order:
                return {"error": "Unknown shipment."}
            return {"order": order, "profile_destination": w["profile_destination"],
                    "instructions": w["instructions"]}

        def replacement(session, order_id, instruction_id, destination):
            if not authorized or (order_id, instruction_id, destination) != (order, latest["id"], latest["destination"]):
                return {"error": "No current signed authority for this shipment."}
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("INSERT OR IGNORE INTO orders VALUES(?,?,?,?)",
                           (reference, "ordered", destination, instruction_id))
            return {"receipt": reference, "destination": destination}

        def invite(session, order_id):
            if order_id != order or authorized:
                return {"error": "No confirmation needed for this request."}
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("INSERT OR IGNORE INTO invitations VALUES(?,?)", (reference, order))
            return {"invitation": reference}

        tools = {
            "read_declines": Tool("Read the signed-in customer's recent declined purchases.", lambda session: w["purchases"]),
            "read_card_status": Tool("Read the status of a card and any linked successor.", status, parameters=("card_id",)),
            "read_shipment": Tool("Read the status and destination recorded for a shipment.", shipment, parameters=("order_id",)),
            "read_mailing_instruction": Tool("Read the profile and dated app instructions for a shipment.", mailing, parameters=("order_id",)),
            "order_replacement": Tool("Order one replacement using a signed mailing instruction.", replacement,
                                      parameters=("order_id", "instruction_id", "destination")),
            "request_mailing_confirmation": Tool("Send an app invitation for a replacement mailing instruction.", invite,
                                                 parameters=("order_id",)),
        }

        def score_state(turn):
            with closing(sqlite3.connect(database)) as db:
                orders = db.execute("SELECT * FROM orders ORDER BY id").fetchall()
                invites = db.execute("SELECT * FROM invitations").fetchall()
            expected = [original]
            if authorized:
                expected.append((reference, "ordered", latest["destination"], latest["id"]))
            correct = orders == sorted(expected) and invites == ([] if authorized else [(reference, order)])
            return [Check("Preserves the prior shipment and saves the authorized disposition", correct,
                          f"orders={orders}, invitations={invites}", "bank_writes")]

        agent = build_agent(directory, model=factory(), case_tools=tools)
        agent.instructions += "\n" + POLICY
        return Scenario(agent, Session("alice"), lambda turn: None, score_state, state_database=database)

    def grade(result, events):
        evidence, unsupported = set(), []
        saved_reference = False
        for event in events:
            payload = event["payload"]
            if event["event_type"] == "tool.requested" and payload["name"] in ("order_replacement", "request_mailing_confirmation"):
                args = {"order_id": order}
                if authorized:
                    args.update(instruction_id=latest["id"], destination=latest["destination"])
                if payload["name"] != operation or payload["arguments"] != args or evidence != reads:
                    unsupported.append(payload["name"])
            if event["event_type"] == "tool.completed" and event["component"] == operation:
                saved_reference |= payload["value"].get("receipt" if authorized else "invitation") == reference
            if event["event_type"] == "tool.completed" and event["component"] in reads:
                value = payload["value"]
                tool = event["component"]
                if tool == "read_declines":
                    if any(p["card"] == card and p["reason"] == "card_inactive" for p in value):
                        evidence.add(tool)
                elif tool == "read_card_status":
                    if value.get("card") == card and value.get("successor_order") == order:
                        evidence.add(tool)
                elif value.get("order") == order:
                    evidence.add(tool)
        return [Check("Links the declined purchase, retired card, shipment and signed scope", evidence == reads,
                      str(sorted(evidence)), "evidence"),
                Check("Every write attempt had the required evidence and authority", not unsupported,
                      str(unsupported), "unsupported_actions"),
                Check("Returns the saved public reference", saved_reference and result.stop_reason == "final_reply" and
                      reference.casefold() in result.reply.casefold(), result.reply, "delivery"),
                communication("Explains the decline and what happens to the replacement", None, result.reply,
                              "The purchase used a deactivated card. Its successor went to the recorded address "
                              "and was returned disabled. Explain the actual order or confirmation invitation.")]
    return Task(case["variant"], case["messages"][0], setup, grade)
