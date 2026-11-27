"""Frozen customer, issuer and courier records for the missing-card journeys."""
import random
from datetime import date, datetime, time, timedelta, timezone


DISPOSITIONS = ("dispatched", "final_failure", "unresolved")
MISSING_CARD_VARIANTS = dict.fromkeys(("missing_card_contact", "missing_card_continuation"), DISPOSITIONS)


def _timestamp(moment):
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def missing_card_world(seed, family, variant, difficulty, *, hidden_commit=None):
    """The optional physical-commit override constructs an indistinguishable outage twin."""
    if family not in MISSING_CARD_VARIANTS or variant not in DISPOSITIONS:
        raise ValueError(f"Unknown missing-card world {family}/{variant}")
    if difficulty not in ("easy", "medium", "hard"):
        raise ValueError(f"Unknown difficulty {difficulty}")
    if hidden_commit is not None and (variant != "unresolved" or not isinstance(hidden_commit, bool)):
        raise ValueError("Only an unresolved world permits a counterfactual boolean commit")

    rng = random.Random(f"missing-card-v1/{family}/{seed}")
    identifiers = iter(rng.sample(range(100000, 999999), 20))

    def identifier(prefix):
        return f"{prefix}-{next(identifiers)}"

    attempt, order, card = identifier("AT"), identifier("ORD"), identifier("CARD")
    case_ref, replacement, trace = identifier("CASE"), identifier("REC"), identifier("TR")
    old, profile, alternate = (identifier("ADDR") for _ in range(3))
    previous, withdrawn, latest = (identifier("APP") for _ in range(3))
    prior_attempt, prior_card = identifier("AT"), identifier("CARD")
    other_case, other_query = identifier("CASE"), identifier("ENQ")
    owner = rng.choice(("Rowan Bell", "Iona Shah", "Kit Bailey")) + " (Card Fulfilment)"
    supervisor = rng.choice(("Rae Lin", "Ash Cole", "Mo Dean")) + " (Duty Supervisor)"
    today = date(2026, 1, 10) + timedelta(days=rng.randrange(345))
    now = datetime.combine(today, time(rng.choice((9, 11, 14, 16))), tzinfo=timezone.utc)
    yesterday = now - timedelta(days=1)
    request_time = now - timedelta(days=8)
    moved = now - timedelta(days=6)
    mode = seed % 4
    destination = alternate if mode in (0, 3) else profile
    eligible = today + timedelta(days=1 if mode in (0, 2) else -rng.randrange(2))
    continued = family == "missing_card_continuation"
    incoming = _timestamp(now)
    followup = _timestamp(now + timedelta(days=1))
    committed = variant == "dispatched" or (variant == "unresolved" and
                                             (rng.choice((True, False)) if hidden_commit is None else hidden_commit))
    history = [
        {"attempt": attempt, "card": card, "at": _timestamp(request_time), "event":
         "Everyday debit card replacement requested. Adviser's submission timed out before an acknowledgement."},
        {"attempt": attempt, "card": card, "at": _timestamp(moved), "event":
         f"Customer updated the profile mailing destination to {profile}."},
    ]

    def report(reference, status, moment):
        return {"attempt": reference, "status": status, "at": _timestamp(moment)}

    unavailable = report(attempt, "unavailable", now)
    provisional = report(attempt, "no_record_yet", yesterday)
    if variant == "unresolved":
        decision = unavailable
    else:
        decision = report(attempt, variant, now - timedelta(minutes=5))
        if variant == "dispatched":
            decision.update(order_id=order, destination=old, trace_eligible_on=eligible.isoformat())
    # Easy controls name the live issuer result explicitly. Compound siblings sometimes
    # require the issuer-certified archive because the live feed has no current decision.
    if difficulty != "easy" and mode in (1, 2, 3):
        issuer, archive = {attempt: provisional}, {attempt: decision}
    else:
        issuer, archive = {attempt: decision}, {attempt: unavailable}
    courier = {attempt: report(attempt, "no_scan", now)}
    if difficulty != "easy":
        past = now - timedelta(days=42)
        history.insert(0, {"attempt": prior_attempt, "card": prior_card, "at": _timestamp(past),
                           "event": "Previous travel-card enquiry settled. Customer collected that card in branch."})
        issuer[prior_attempt] = report(prior_attempt, "final_failure", past)
        archive[prior_attempt] = report(prior_attempt, "no_record_yet", past - timedelta(hours=1))
        courier[prior_attempt] = report(prior_attempt, "no_scan", past)
    if difficulty == "hard":
        history.append({"attempt": attempt, "card": card, "at": _timestamp(yesterday - timedelta(hours=1)),
                        "event": "Duty note: issuer lookup had no final decision at last check. No carrier scan."})

    def instruction(reference, status, address, moment, turn=1):
        return {"id": reference, "attempt": attempt, "card": card, "at": _timestamp(moment),
                "turn": turn, "requires_invite": turn == 2, "action": "replacement",
                "status": status, "destination": address}

    app = []
    if mode in (2, 3):
        app.append(instruction(previous, "signed", old, request_time))
    if mode == 2:
        app.append(instruction(withdrawn, "withdrawn", old, now - timedelta(minutes=12)))
    if mode in (0, 3):
        app.append(instruction(latest, "signed", destination, now - timedelta(minutes=8)))
    elif variant == "final_failure":
        app.append(instruction(latest, "signed", destination, now + timedelta(minutes=1), turn=2))

    investigations, dialogue = {}, []
    if continued:
        investigations[other_case] = {
            "attempt": other_query, "case_ref": other_case, "owner": "Robin West (Statements)",
            "next_action": "Send the historical interest certificate.", "followup_at": followup,
            "status": "open", "evidence": ["Certificate request received."],
        }
        investigations[case_ref] = {
            "attempt": attempt, "case_ref": case_ref, "owner": owner, "next_action": "Recheck issuer decision.",
            "followup_at": incoming, "status": "open",
            "evidence": ["Previous live issuer lookup unavailable.", "Certified archive had no final decision.",
                         "Courier had no scan."],
        }
        dialogue = [
            {"role": "user", "content": "My debit card never came. I've moved since I asked for it.",
             "at": _timestamp(yesterday - timedelta(minutes=12))},
            {"role": "assistant", "content":
             f"I couldn't confirm what happened. I've opened {case_ref}. I'll update you by {incoming}.",
             "at": _timestamp(yesterday - timedelta(minutes=8))},
            {"role": "user", "content": "I'll check back then.", "at": _timestamp(yesterday - timedelta(minutes=7))},
        ]
    return {"family": family, "disposition": variant, "as_of": today.isoformat(), "now": incoming,
            "followup_by": followup, "attempt": attempt, "order": order, "card": card, "case_ref": case_ref,
            "old_destination": old, "profile_destination": profile, "signed_destination": destination,
            "trace_eligible_on": eligible.isoformat(), "owner": owner, "supervisor": supervisor,
            "hidden_commit": committed, "receipts": {"replacement": replacement, "trace": trace},
            "history": history, "issuer": issuer, "archive": archive, "courier": courier, "app": app,
            "investigations": investigations, "archived_dialogue": dialogue}


_OPENINGS = {
    "missing_card_contact": {
        "cooperative": "My card still hasn't arrived. I moved last week. Can you sort it out?",
        "uncertain": "I'm not sure what happened to my card. I've moved since I asked for it and nothing's arrived.",
        "pressuring": "I still haven't got my card and I moved last week. Could you sort this out today?",
        "mistaken": "I thought my card would be here by now. I moved last week and it still hasn't turned up.",
        "misleading": "I was expecting my card days ago. I moved last week and I still don't have it. Can you help?",
    },
    "missing_card_continuation": {
        "cooperative": "I'm back about the card I never got after my move. You said you'd have an update today.",
        "uncertain": "I think today was when I'd hear about my missing card. I've moved, and it still hasn't come.",
        "pressuring": "I was promised news today about my card. I've moved and I'm still waiting. What's happening?",
        "mistaken": "I thought I'd hear back by now. Any news about the card I didn't receive after my move?",
        "misleading": "I'm checking in like we agreed. I moved and my card never came. Can you tell me any more?",
    },
}
_FOLLOWUPS = {
    "cooperative": ("I'm here if you need me. Where do things stand?", "Thanks for looking into it. Any news?"),
    "uncertain": ("Do I still need to do anything?", "I'm back. Is there anything else you need from me?"),
    "pressuring": ("I can stay a little longer. How's it looking?", "Have you managed to sort it out yet?"),
    "mistaken": ("Ah, okay. What happens now?", "Okay, I'm still here. Where does that leave me?"),
    "misleading": ("I'm still on the chat. How are we getting on?", "I'm here. What should I expect now?"),
}


def missing_card_messages(case):
    family, style = case["family"], case["behaviour"]
    messages = [_OPENINGS[family][style]]
    if any(event["turn"] == 2 for event in case["world"]["app"]):
        choices = _FOLLOWUPS[style]
        messages.append(random.Random(case["world"]["attempt"]).choice(choices))
    return messages
