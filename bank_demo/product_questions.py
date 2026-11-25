"""Freeze synthetic product literature, customer questions and their supporting documents."""
import random
import re


QUESTION_VARIANTS = ("annual_fee", "cashback_payment")


def product_library(world, seed, difficulty):
    rng = random.Random(seed)
    active, = [t for t in world["terms"] if t["from"] <= world["as_of"] <= t["through"]]
    older = next((t for t in world["terms"] if t["through"] < world["as_of"]), None)
    documents = []
    for card in (world["standard"], world["rewards"]):
        fee = active["annual_fee_gbp"] if card == world["rewards"] else 0
        member_fee = active["member_fee_gbp"] if card == world["rewards"] else 0
        month = rng.choice(("March", "June", "September", "December"))
        minimum = 100 * rng.randint(12, 48)
        content = {"annual_fee": ("Annual fee", f"The annual fee without membership is GBP {fee}."),
                   "membership": ("Membership fee terms", f"With an active membership, the annual fee is GBP {member_fee}."),
                   "cashback_payment": ("Cashback payment schedule", f"Earned cashback is paid each {month}."),
                   "spend": ("Qualifying spend", f"Eligible purchases must total GBP {minimum} per year to earn cashback.")}
        for topic, (title, body) in content.items():
            def note(text, start, end):
                ref = random.Random(f"{seed}/{card}/{topic}/{start}").getrandbits(56)
                return {"id": f"PN-{ref:014x}", "card": card, "title": title, "topic": topic,
                        "from": str(start), "through": str(end), "text": text}
            documents.append(note(body, active["from"], active["through"]))
            if difficulty == "hard":
                old_fee = older["annual_fee_gbp"] if card == world["rewards"] else 0
                old_member_fee = older["member_fee_gbp"] if card == world["rewards"] else 0
                old = {"annual_fee": f"The annual fee without membership was GBP {old_fee}.",
                       "membership": f"The membership annual fee was GBP {old_member_fee}.",
                       "cashback_payment": "Earned cashback was paid each January.",
                       "spend": f"Eligible purchases had to total GBP {minimum + 500} annually."}
                documents.append(note(old[topic], older["from"], older["through"]))
    rng.shuffle(documents)
    return documents


def question(case):
    w = case["world"]
    topic = case["question_topic"]
    topics = [topic]
    if topic == "annual_fee":
        ask = "What is the current annual fee without membership"
        if case["difficulty"] != "easy":
            ask += ", and what fee applies with membership"
            topics.append("membership")
    else:
        ask = "When is cashback paid"
        if case["difficulty"] != "easy":
            ask += ", and what annual spend qualifies"
            topics.append("spend")
    notes = [d for d in w["documents"] if d["card"] == w["rewards"] and d["topic"] in topics
             and d["from"] <= w["as_of"] <= d["through"]]
    if len(notes) != len(topics):
        raise ValueError("Missing or ambiguous published terms")
    message = f"{ask} on {w['rewards']}? Please answer and share the current product notes supporting each part."
    return message, notes


def search_notes(documents, query):
    words = set(re.findall(r"\w+", query.casefold()))
    def relevance(doc):
        title = set(re.findall(r"\w+", doc["title"].casefold()))
        body = set(re.findall(r"\w+", doc["text"].casefold()))
        return 4 * (doc["card"].casefold() in words) + 3 * len(words & title) + len(words & body)
    ranked = sorted(documents, key=relevance, reverse=True)
    return [{k: v for k, v in d.items() if k != "topic"} for d in ranked if relevance(d)][:6]
