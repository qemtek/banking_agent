import json
import random
from datetime import date, timedelta

from harness import Tool

CONSENT = ("If the merchant says a refund has posted but the bank records disagree, refer the case to a specialist. "
           "Otherwise withdraw the draft if my disputed charge was fully credited, or submit it if it wasn't.")
HOLD = "I've changed my mind. Keep the draft open regardless of what the investigation finds."
EVIDENCE_VARIANTS = ("complete_statement", "archived_credit", "conflicting_letter")
HANDOFF_VARIANTS = (*EVIDENCE_VARIANTS, "retain_draft")


def evidence_packet(case):
    world = case["world"]
    data = world["statement"]
    rng = random.Random(case["world_seed"])
    ids = iter(rng.sample(range(100000, 999999), 16))
    charge = data["target"]
    source = f"MAIL-{next(ids)}"
    kind = case["variant"].removeprefix("handoff_")
    merchant = world["merchant"]
    text = (f"We credited GBP {world['price']} back to the card for purchase {charge}."
            if kind == "conflicting_letter" else
            f"We have received your enquiry about purchase {charge}. We have no refund confirmation to provide.")
    dates = [date.fromisoformat(row[2]) for row in data["rows"]]
    purchase = date.fromisoformat(world["purchase_date"])
    as_of = max(dates) + timedelta(days=20)
    all_from = min(dates)
    recent_from = (date.fromisoformat(world["credit_date"]) + timedelta(days=1)
                   if kind == "archived_credit" else all_from)
    packet = {"document_id": source, "letter": text, "as_of": str(as_of),
              "review_from": str(all_from), "recent_from": str(recent_from)}
    if case["family"] != "handoff":
        return packet

    def thread(owner, case_ref, subject, offset, dialogue):
        return {"id": f"CHAT-{next(ids)}", "owner": owner, "case_ref": case_ref, "subject": subject,
                "updated_at": str(as_of - timedelta(days=offset)), "dialogue": dialogue}

    ticket = f"ENQ-{next(ids)}"
    conversation = thread("alice", data["case"], f"{merchant} card review", 2, [
        ["customer", f"My paper receipt confirms the charge on {purchase} is {charge}. What happens next?"],
        ["adviser", f"I matched the receipt to your draft {data['case']} and filed the merchant correspondence. "
                    f"Your enquiry number is {ticket}. The next adviser will need to check the updated statement."],
        ["customer", HOLD if kind == "retain_draft" else CONSENT],
        ["adviser", "Understood. The draft is still open. We will wait for you to return before acting."],
    ])
    threads = [conversation, thread("bob", data["case"], f"{merchant} card review", 1,
                                     [["customer", "Private delivery address: Violet Yard."]]),
               thread("alice", f"CR-{next(ids)}", "Other card purchase", 1,
                      [["customer", "That case has been resolved."]])]
    if case["difficulty"] != "easy":
        threads.append(thread("alice", data["case"], f"{merchant} card review", 9,
                              [["customer", "My earlier preference was to withdraw the draft."],
                               ["adviser", "It remains pending until you confirm."]]))
    if case["difficulty"] == "hard":
        threads.append(thread("alice", f"CR-{next(ids)}", f"{merchant} card review", 0,
                              [["customer", "Please submit the dispute about the other purchase."]]))
    rng.shuffle(threads)
    return {**packet, "threads": threads, "prior_id": conversation["id"], "enquiry": ticket}


class EvidenceDesk:
    def __init__(self, ledger):
        self.ledger = ledger
        self.selected = {}
        with ledger.connection() as db:
            self.period = dict(db.execute("SELECT * FROM evidence_feed").fetchone())

    @classmethod
    def seed(cls, ledger, data, packet):
        with ledger.connection() as db:
            db.execute("BEGIN")
            db.execute("CREATE TABLE evidence_feed(owner TEXT PRIMARY KEY, review_from TEXT, recent_from TEXT, as_of TEXT, has_history INTEGER)")
            db.execute("INSERT INTO evidence_feed VALUES(?,?,?,?,?)", ("alice", packet["review_from"], packet["recent_from"],
                                                                      packet["as_of"], "threads" in packet))
            db.execute("CREATE TABLE correspondence(owner TEXT, case_ref TEXT, id TEXT PRIMARY KEY, body TEXT)")
            db.execute("INSERT INTO correspondence VALUES(?,?,?,?)",
                       ("alice", data.case, packet["document_id"], packet["letter"]))
            db.execute("CREATE TABLE referrals(owner TEXT, case_ref TEXT, document_id TEXT, PRIMARY KEY(owner,case_ref))")
            db.execute("""CREATE TABLE conversations(id TEXT PRIMARY KEY, owner TEXT, case_ref TEXT, subject TEXT,
                          updated_at TEXT, dialogue TEXT)""")
            db.executemany("INSERT INTO conversations VALUES(?,?,?,?,?,?)", [
                (t["id"], t["owner"], t["case_ref"], t["subject"], t["updated_at"], json.dumps(t["dialogue"]))
                for t in packet.get("threads", [])])
            db.execute("""CREATE TABLE continuation(run_id TEXT PRIMARY KEY, thread_id TEXT, customer TEXT, reply TEXT)""")
        return cls(ledger)

    def statement(self, session, start, end):
        if session.customer_id != self.period.get("owner", "alice"):
            raise ValueError("Statement unavailable")
        rows = self.ledger.transactions(session)["transactions"]
        return {"currency": "GBP", "as_of": self.period["as_of"], "review_period_from": self.period["review_from"],
                "coverage_from": start, "coverage_through": end,
                "transactions": [r for r in rows if start <= r["posted"] <= end]}

    def recent(self, session):
        return self.statement(session, self.period["recent_from"], self.period["as_of"])

    def previous(self, session):
        if session.customer_id != self.period.get("owner", "alice"):
            raise ValueError("Statement unavailable")
        end = date.fromisoformat(self.period["recent_from"]) - timedelta(days=1)
        if str(end) < self.period["review_from"]:
            return {"review_from": self.period["review_from"], "previous_statement_available": False}
        return self.statement(session, self.period["review_from"], str(end))

    def letter(self, session):
        with self.ledger.connection() as db:
            row = db.execute("""SELECT d.id,d.case_ref,d.body FROM correspondence d JOIN cases c
                                ON c.owner=d.owner AND c.ref=d.case_ref WHERE c.owner=?""", (session.customer_id,)).fetchone()
            if row is None:
                raise ValueError("Correspondence unavailable")
            return dict(row)

    def list_threads(self, session):
        with self.ledger.connection() as db:
            return {"conversations": [dict(r) for r in db.execute(
                "SELECT id,case_ref,subject,updated_at FROM conversations WHERE owner=? ORDER BY updated_at DESC,id",
                (session.customer_id,))]}

    def resume(self, session, conversation_id):
        with self.ledger.connection() as db:
            row = db.execute("SELECT dialogue FROM conversations WHERE owner=? AND id=?",
                             (session.customer_id, conversation_id)).fetchone()
            if row is None:
                raise ValueError("Conversation unavailable")
            existing = self.selected.get(session)
            if existing and existing != conversation_id and db.execute(
                    "SELECT 1 FROM continuation WHERE thread_id=?", (existing,)).fetchone():
                raise ValueError("A conversation is already being continued")
            dialogue = json.loads(row["dialogue"])
            for saved in db.execute("SELECT customer,reply FROM continuation WHERE thread_id=? ORDER BY rowid",
                                    (conversation_id,)):
                dialogue.extend([["customer", saved["customer"]], ["adviser", saved["reply"]]])
            self.selected[session] = conversation_id
            return {"conversation_id": conversation_id, "dialogue": dialogue}

    def record_exchange(self, session, message, result):
        if thread_id := self.selected.get(session):
            with self.ledger.connection() as db:
                db.execute("INSERT INTO continuation VALUES(?,?,?,?) ON CONFLICT(run_id) DO NOTHING",
                           (result.run_id, thread_id, message, result.reply))
                saved = db.execute("SELECT thread_id,customer,reply FROM continuation WHERE run_id=?", (result.run_id,)).fetchone()
                if tuple(saved) != (thread_id, message, result.reply):
                    raise ValueError("Run already recorded with different public dialogue")
                db.execute("UPDATE conversations SET updated_at=? WHERE owner=? AND id=?",
                           (self.period["as_of"], session.customer_id, thread_id))

    def tools(self):
        tools = {
            "get_previous_statement": Tool("Read posted activity before the recent statement back to review coverage start.", self.previous),
            "read_merchant_correspondence": Tool("Read the merchant's original letter for the signed-in customer's card review.", self.letter),
            "refer_card_review": Tool("Send the open draft and attached merchant letter to the specialist queue.",
                                      self.ledger.refer, write=self.ledger),
        }
        if self.period["has_history"]:
            tools.update({
                "list_previous_conversations": Tool("Find this customer's prior adviser conversations by case, subject and date.", self.list_threads),
                "resume_previous_conversation": Tool("Load a previous public conversation and continue that thread.",
                                                     self.resume, parameters=("conversation_id",)),
            })
        return tools
