"""Measure customer-visible replacement outcomes from saved development trials."""
import argparse
import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from evaluation import Turn, copy_database
from evaluation.casebooks import digest, trial_directory

from .card_servicing_tasks import expected_operations, stages
from .instances import materialize, validate
from .missing_card_tasks import active_signature, disposition
from .rescore import read_saved_turn


SERVICING = {"card_reissue", "card_identity"}
FAMILIES = SERVICING | {"missing_card_contact", "missing_card_continuation"}
CORRECT, INCORRECT, UNKNOWN = "verified correct", "verified incorrect", "unresolved"
ACK = r"(?:ok(?:ay)?[,! .]*)?(?:thanks|thank you)(?: (?:very much|so much|for (?:your help|checking|the update)))?(?:, that answers my question)?[.! ]*"
REFERENCE = re.compile(r"\b(?:cs|rec)-\d+\b", re.I)


def load(path):
    return json.loads(path.read_text())


def normal(text):
    return " ".join(text.casefold().replace("’", "'").split())


def acknowledges(customer, receipt, order):
    text = normal(customer)
    ref, target = re.escape(receipt.lower()), re.escape(order.lower())
    recorded = (rf"(?:thanks|thank you) for confirming the replacement for order {target}[.;] "
                rf"(?:i've noted reference|the reference is) {ref}[.!]?")
    received = rf"thanks, i have the confirmed replacement reference {ref} for order {target}[.!]?"
    return any(re.fullmatch(pattern, text) for pattern in (ACK, recorded, received))


def outcome_claims(raw, receipt, order, destination, committed, observed, attached, reread):
    """Accept whole evidence-backed clauses. Anything outside the grammar stays open."""
    text = normal(raw)
    for value, symbol in ((receipt, "RECEIPT"), (order, "ORDER"), (destination, "ADDRESS")):
        if value:
            text = re.sub(rf"(?<![\w-]){re.escape(value.casefold())}(?![\w-])", symbol, text)
    subject = r"(?:(?:the|your|a|one) replacement(?: card| request)?(?: for order ORDER)?)"
    location = (r"(?: for order ORDER| (?:for|to) (?:your|the) confirmed destination(?:, ADDRESS)?"
                r"| to the address (?:in your signed (?:app )?instruction|you signed for in the app))?")
    positive = (rf"(?:yes, )?{subject} (?:has (?:already |now )?been |was (?:successfully )?|is (?:already )?)"
                rf"(?:recorded|ordered|requested|completed){location}")
    proof = r"(?:(?:the|your) (?:issuer |replacement |confirmation |receipt )?(?:receipt|reference) is RECEIPT)"
    shared = r"(?:i've (?:attached|shared) (?:it (?:here|to this conversation)|the issuer receipt: RECEIPT)|it has been attached to this conversation)"
    duplicate = r"there's no need to (?:place another (?:request|order)|order another(?: one| card)?)"
    claims = re.split(r"[.!;\n]+|, (?:and|so) | and (?=(?:the|your) (?:receipt|issuer receipt))", text)
    has_confirmation = attached
    unknown = False
    for clause in filter(None, (part.strip() for part in claims)):
        if re.fullmatch(positive, clause):
            if not committed:
                return INCORRECT, "completion claimed before commitment"
            has_confirmation = True
            unknown |= not observed
        elif re.fullmatch(proof, clause):
            if not committed:
                return INCORRECT, "receipt claimed before commitment"
            unknown |= not observed
        elif re.fullmatch(r"(?:the|your) order reference is ORDER", clause):
            unknown |= not observed
        elif re.fullmatch(shared, clause):
            unknown |= not attached
        elif re.fullmatch(duplicate, clause):
            unknown |= not committed
        elif clause == "i checked the saved order again":
            if not reread:
                return INCORRECT, "claimed a fresh check without a lookup"
        else:
            unknown = True
    if unknown or not has_confirmation:
        return UNKNOWN, "free-text outcome needs interpretation"
    return CORRECT, "replacement and customer notice verified"


def ledger_facts(case, db, n):
    w = case["world"]
    if case["family"] in SERVICING:
        wanted = stages(case)[n-1] in ("resolve", "reconcile", "verify_again") and "replacement" in expected_operations(w)
        row = db.execute("SELECT destination,receipt FROM receipts WHERE operation='replacement' AND order_id=?",
                         (w["order"],)).fetchone()
        return wanted, row, w["old_address"]
    if "instructions" in w:
        sig = max(w["instructions"], key=lambda i: i["at"])
        row = db.execute("SELECT destination,id FROM orders WHERE status='ordered' AND authority=?", (sig["id"],)).fetchone()
        return sig["status"] == "signed", row, sig["destination"]
    invites = [r[0] for r in db.execute("SELECT turn FROM invitations WHERE attempt=?", (w["attempt"],))]
    sig = active_signature(w, n, invites)
    row = db.execute("SELECT destination,receipt FROM orders WHERE origin='replacement' AND attempt=? AND order_id=?",
                     (w["attempt"], w["order"])).fetchone()
    return disposition(w, sig) == "replacement", row, sig and sig["destination"]


def grade_turn(case, n, customer, public, raw, events, state, facts, known):
    w = case["world"]
    wanted, row, destination = facts
    receipt = w.get("receipt") or w["receipts"]["replacement"]
    committed = row == (destination, receipt)
    ack = acknowledges(customer, receipt, w["order"])
    scripted = customer == case["messages"][n-1]
    recheck = bool(re.search(r"\b(?:check|look (?:up|at)|verify|recheck)\b", normal(customer)) and
                   re.search(r"\b(?:again|once more|saved (?:order|result))\b", normal(customer)))
    results = [(e["component"], e["payload"]["value"]) for e in events if e["event_type"] == "tool.completed"]
    reread = any(k == "read_servicing_order" and v.get("order") == w["order"] for k, v in results)
    for tool, v in results:
        if not isinstance(v, dict) or "error" in v:
            continue
        if tool in {"service_replacement", "read_servicing_order", "share_servicing_receipt"} and v.get("order") == w["order"]:
            known.update(v.get("receipts", {}).values())
            if v.get("receipt"):
                known.add(v["receipt"])
        if (tool == "issue_card_replacement" and v.get("attempt") == w.get("attempt")) or tool == "order_replacement":
            known.add(v.get("receipt"))
    attachment = public.removesuffix(raw)
    attached = receipt.lower() in {s.lower() for s in REFERENCE.findall(attachment)}
    refs = {s.upper() for s in REFERENCE.findall(public)}
    actual_receipt = receipt in refs
    policy = next(e["payload"]["messages"][0]["content"] for e in events if e["event_type"] == "model.requested")
    required = ("receipt field returned by the issuer" in policy or "Return the bank receipt for every completed" in policy
                or "receipt" in normal(customer) or "instructions" in w)

    def result(verdict, reason):
        return {"verdict": verdict, "reason": reason, "committed": committed, "public_receipt": actual_receipt,
                "customer": customer, "turn": n, "issuer_receipt_seen": receipt in known}

    claim, reason = outcome_claims(raw, receipt, w["order"], destination, committed, receipt in known, attached, reread)
    if claim == INCORRECT:
        if reason == "completion claimed before commitment" and (w.get("hidden_commit") or "instructions" in w):
            return result(UNKNOWN, "possible reference to the earlier shipment")
        return result(claim, reason)
    if not ack and scripted and any(c.category == "bank_writes" and c.passed is False for c in state):
        return result(INCORRECT, "authorized disposition missing or wrong in ledger")
    named = re.findall(r"(?:^|[.!]\s+|, and )(?:(?:the|your) )?(?:issuer|replacement) receipt"
                       r"(?: is|:)\s*((?:CS|REC|ORD)-\d+)(?=[.!]|$)", raw, re.I)
    if wanted and committed and any(ref.upper() != receipt for ref in named):
        if actual_receipt:
            return result(UNKNOWN, "conflicting public receipt statements")
        return result(INCORRECT, "wrong identifier explicitly given as the receipt")
    if recheck and scripted and case["family"] in SERVICING and not reread:
        return result(INCORRECT, "requested fresh lookup missing")
    courtesy = re.fullmatch(r"(?:you're welcome|my pleasure|happy to help)[.! ]*", normal(raw))
    if ack and courtesy and not attachment and all(c.passed is not False for c in state):
        return result(CORRECT, "acknowledgement")
    if not scripted and not ack:
        return result(UNKNOWN, "customer request needs interpretation")
    if ack:
        return result(UNKNOWN, "additional claim in acknowledgement")
    if not wanted or not committed:
        return result(UNKNOWN, "pending, prior or alternative disposition needs interpretation")
    if not actual_receipt:
        if case["family"] in SERVICING and stages(case)[n-1] == "resolve" and w["ack_failure"] and receipt not in known:
            return result(UNKNOWN, "issuer acknowledgement pending")
        if required:
            return result(INCORRECT, "required issuer receipt missing from public reply")
        if re.search(rf"\b{re.escape(w['order'])}\b", public, re.I):
            return result(UNKNOWN, "historical public-reference instruction ambiguous")
        return result(UNKNOWN, "public reference or customer instruction needs interpretation")
    if receipt not in known or refs != {receipt} or any(c.passed is False for c in state):
        return result(UNKNOWN, "competing references or incomplete supporting evidence")
    if claim == CORRECT and ("instructions" in w or
                             (case["family"] == "missing_card_continuation" and n == 1)):
        return result(UNKNOWN, "additional customer question needs interpretation")
    return result(claim, reason)


def evaluate(case, row, trial, frozen):
    task = materialize(case, lambda: None)
    turns = (Turn(task.message, task.score), *task.followups)
    evidence, history, known = [], [], set()
    dialogue = []
    cid = None
    if row["status"] == "ERROR":
        raise ValueError("saved execution error")
    paths = [trial / Path(p).name for p in row["turn_audits"]]
    if frozen:
        paths.insert(0, trial / "opening.jsonl")
    if len(paths) != len(turns):
        raise ValueError("incomplete turn evidence")
    with TemporaryDirectory() as scratch:
        fixture = task.setup(Path(scratch))
        for n, (turn, path) in enumerate(zip(turns, paths), 1):
            if frozen and n == 1:
                _, events = read_saved_turn(path, None, n, renamed_opening=True)
                cid = events[0]["payload"]["conversation_id"]
                history.append(events)
                continue
            raw, events = read_saved_turn(path, cid, n)
            cid = events[0]["payload"]["conversation_id"]
            customer = events[0]["payload"]["message"]
            public = task.render_reply(raw, events) if task.render_reply else raw
            dialogue.extend([{"speaker": "customer", "text": customer}, {"speaker": "bank", "text": public.reply}])
            snapshot = trial / "snapshots" / (path.stem + ".sqlite")
            if frozen:
                ledgers = list(trial.glob("ledger-*.sqlite"))
                if len(ledgers) != 1:
                    raise ValueError("missing unique frozen return ledger")
                snapshot = ledgers[0]
            if not snapshot.is_file():
                raise ValueError("missing turn snapshot")
            copy_database(snapshot, fixture.state_database)
            state = fixture.score_state(n)
            checks = task.score_turn(turn, public, events, history)
            with closing(sqlite3.connect(fixture.state_database)) as db:
                facts = ledger_facts(case, db, n)
            assessment = grade_turn(case, n, customer, public.reply, raw.reply, events, state, facts, known)
            failed = {c.criterion for c in checks if c.passed is False}
            if assessment["reason"] == "acknowledgement":
                failed -= {"Rechecked the saved replacement", "Read the saved order when asked and before any retry",
                           "Delivers the requested public operation receipts"}
            if assessment["verdict"] == CORRECT and failed:
                assessment.update(verdict=UNKNOWN, reason="additional task criterion needs interpretation")
            evidence.append(assessment)
    if dialogue != row["dialogue"]:
        raise ValueError("saved public dialogue differs from replay")
    if row.get("customer_fidelity") == "deviated":
        return {"verdict": UNKNOWN, "reason": "customer simulator deviation", "turns": evidence}
    decisive = next((e for e in evidence if e["verdict"] == INCORRECT),
                    next((e for e in evidence if e["verdict"] == UNKNOWN), evidence[-1]))
    return {"verdict": decisive["verdict"], "reason": decisive["reason"], "turns": evidence}


def report(root):
    rows, sources = [], {}
    def include(batch, case, row, trial, frozen=False):
        try:
            assessment = evaluate(case, row, trial, frozen)
        except (OSError, ValueError, KeyError, sqlite3.Error, StopIteration) as exc:
            assessment = {"verdict": UNKNOWN, "reason": f"missing or inconsistent replay evidence: {exc}", "turns": []}
        rows.append({"batch": str(batch.relative_to(root)), "case_id": case["case_id"], "repeat": row["repeat"],
                     "family": case["family"], "cohort": "frozen returns" if frozen else "full conversations",
                     "wording": row.get("wording"), **assessment})

    for path in sorted([*root.glob("evaluations/*/manifest.json"), *root.glob("experiments/*/*/manifest.json")]):
        meta = load(path)
        if (meta.get("split"), meta.get("model"), meta.get("reasoning_effort"), meta.get("suite")) != (
                "development", "gpt-6.1-terra", "medium", "generated"):
            continue
        batch = path.parent
        book = load(batch / "casebook.json")
        if book["split"] != "development" or digest(book) != meta["casebook_id"]:
            raise ValueError("development casebook mismatch")
        cases = {c["case_id"]: c for c in book["cases"] if c["family"] in FAMILIES}
        if not cases:
            continue
        validate(book)
        sources[batch.name] = meta["casebook_id"], cases
        records = {(r["case_id"], r["repeat"]): r for r in load(batch / "report.json")}
        for cid, case in cases.items():
            for repeat in range(1, meta["repeats"] + 1):
                row = records.get((cid, repeat), {"repeat": repeat, "status": "ERROR"})
                include(batch, case, row, trial_directory(batch, row))

    for path in sorted(root.glob("experiments/*/manifest.json")):
        meta = load(path)
        if (meta.get("source_batch") not in sources or meta.get("model") != "gpt-6.1-terra" or
                meta.get("reasoning_effort") != "medium" or not ({"messages_by_case", "messages"} & meta.keys())):
            continue
        origin, cases = sources[meta["source_batch"]]
        if origin != meta["casebook_id"]:
            raise ValueError("frozen casebook mismatch")
        batch = path.parent
        records = {(r["case_id"], r["repeat"], r["wording"]): r for r in load(batch / "report.json")}
        for cid, repeat, wording in meta["schedule"]:
            case = cases[cid]
            row = records.get((cid, repeat, wording), {"repeat": repeat, "status": "ERROR", "wording": wording})
            messages = meta["messages_by_case"][cid] if "messages_by_case" in meta else meta["messages"]
            fork = {**case, "messages": [case["messages"][0], messages[wording]]}
            include(batch, fork, row, batch / "trials" / cid / str(repeat) / wording, True)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audits", type=Path, default=Path("audits"))
    parser.add_argument("--output", type=Path, help="Write individual verdicts and turn evidence as JSON")
    args = parser.parse_args()
    rows = report(args.audits)
    if not rows:
        parser.error("no eligible saved development runs")
    for cohort in sorted({r["cohort"] for r in rows}):
        sample = [r for r in rows if r["cohort"] == cohort]
        counts = Counter(r["verdict"] for r in sample)
        print(f"{cohort} (n={len(sample)})")
        for verdict in (CORRECT, INCORRECT, UNKNOWN):
            print(f"  {verdict}: {counts[verdict]} ({counts[verdict] / len(sample):.1%})")
        issued = [r for r in sample if any(t["committed"] for t in r["turns"])]
        delivered = sum(any(t["committed"] and t["issuer_receipt_seen"] and t["public_receipt"]
                            for t in r["turns"]) for r in issued)
        print(f"  issuer receipt visible on a committed turn: {delivered}/{len(issued)}")
    if args.output:
        args.output.write_text(json.dumps(rows, indent=2) + "\n")


if __name__ == "__main__":
    main()
