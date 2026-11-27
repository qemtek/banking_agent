"""Paired retention study on frozen, multi-statement dispute histories at the normal adapter budget."""

import argparse
import calendar
import json
import random
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import date
from pathlib import Path
from time import monotonic

from adapters.codex_cli import create_model
from evaluation.casebooks import digest
from evaluation.usage import actor_usage, estimate_cost, price_card
from harness import Tool
from harness.checkpoints import SQLiteCheckpoints
from harness.history import TurnHistory
from . import Session, build_agent
from .casebook import source_digest


SUMMARY_TARGET = 32 * 1024

FINAL = (
    "Please finish the invoice dispute we left open for this review, using my latest signed instruction "
    "and the posted credits in our correspondence. Record the resolution and give me the result."
)
MERCHANTS = (
    "Alder Supply",
    "North Mill",
    "Marlin Travel",
    "Valley Fuel",
    "Union Rail",
    "Elm Studio",
    "Town Hardware",
    "Cloud Hosting",
    "Harbour Print",
    "Courier West",
    "Depot Hire",
    "Atlas Parts",
)


def history(seed, keep, linked):
    rng = random.Random(seed)
    ids = rng.sample(range(100000, 999999), 16)
    cases = [f"CR-{n}" for n in ids[:2]]
    charges = [f"TX-{n}" for n in ids[2:4]]
    invoices = [f"INV-{n}" for n in ids[4:6]]
    instructions = [f"SIG-{n}" for n in ids[6:8]]
    credit = f"TX-{ids[8]}"
    amount = 100 * rng.randrange(100, 850) + rng.randrange(1, 100)
    target = seed % 2
    opposite = 1 - target
    credited = target if linked else opposite
    messages = []
    txs = []

    def exchange(question, name, args, value, answer):
        cid = f"archive-{seed}-{len(messages)}"
        messages.extend(
            [
                {"role": "user", "content": question},
                {"role": "assistant", "tool_call": {"name": name, "arguments": args}, "call_id": cid},
                {"role": "tool", "name": name, "call_id": cid, "value": value},
                {"role": "assistant", "content": answer},
            ]
        )

    for month in range(24):
        year, mo = 2023 + month // 12, 1 + month % 12
        period = f"{year}-{mo:02}"
        last = calendar.monthrange(year, mo)[1]
        rows = []
        for j in range(67):
            merchant = rng.choice(MERCHANTS)
            rows.append(
                dict(
                    reference=f"BK-{seed}-{month:02}{j:03}",
                    posted=str(date(year, mo, rng.randint(1, last))),
                    merchant=merchant,
                    amount_pence=-rng.randint(145, 234500),
                    account="Business current",
                    settlement="posted",
                    invoice=f"{merchant[:2].upper()}-{rng.randrange(10000, 99999)}",
                )
            )
        if month == 4:
            for i in range(2):
                rows.append(
                    dict(
                        reference=charges[i],
                        posted=f"{period}-15",
                        merchant="Alder Supply",
                        amount_pence=-amount,
                        account="Business current",
                        settlement="posted",
                        invoice=invoices[i],
                    )
                )
        if month == 18:
            rows.append(
                dict(
                    reference=credit,
                    posted=f"{period}-14",
                    merchant="Alder Supply",
                    amount_pence=amount,
                    account="Business current",
                    settlement="posted",
                    refund_for=charges[credited],
                )
            )
        rows.sort(key=lambda r: (r["posted"], r["reference"]))
        txs.extend(rows)
        vendor = MERCHANTS[month % len(MERCHANTS)]
        debits = [r for r in rows if r["merchant"] == vendor and r["amount_pence"] < 0]
        exchange(
            f"For the {period} expense reconciliation, how many posted debits went to {vendor}, and what was the total?",
            "read_archived_statement",
            {"account": "Business current", "month": period},
            {"currency": "GBP", "period": period, "transactions": rows},
            f"The statement shows {len(debits)} debits to {vendor}, totalling £{-sum(r['amount_pence'] for r in debits) / 100:,.2f}.",
        )
        if month == 4:
            exchange(
                "Two equal Alder Supply invoices are under review. Can you match each invoice to its charge and draft?",
                "read_case_register",
                {},
                [dict(case=cases[i], charge=charges[i], invoice=invoices[i]) for i in range(2)],
                "Both drafts remain open. Each is attached to the charge on its own invoice.",
            )
        if month == 6:
            exchange(
                f"Please earmark {invoices[opposite]} for submission when we finish the review, and wait until then.",
                "read_signed_amendment",
                {},
                dict(reference=instructions[0], invoice=invoices[opposite], disposition="submit_at_close"),
                "I have recorded that instruction and will wait.",
            )
        if month == 10:
            decision = (
                "keep the draft open even if a full refund posts"
                if keep
                else "withdraw the draft only if its own charge has a full posted refund; otherwise keep it open"
            )
            exchange(
                f"Correction: I meant invoice {invoices[target]}, not {invoices[opposite]}. Please {decision}. "
                "This replaces my earlier instruction. Leave the other invoice alone.",
                "read_signed_amendment",
                {},
                dict(
                    reference=instructions[1],
                    supersedes=instructions[0],
                    invoice=invoices[target],
                    disposition="keep_open" if keep else "withdraw_if_fully_refunded",
                ),
                "The corrected instruction is recorded. Both drafts stay open until you ask me to finish.",
            )
        if month == 15:
            exchange(
                "The merchant says it has authorised a reimbursement. Is that a posted credit?",
                "read_merchant_correspondence",
                {},
                dict(
                    reference=f"MA-{ids[9]}",
                    merchant="Alder Supply",
                    invoice=invoices[target],
                    amount_pence=amount,
                    status="authorised_only",
                ),
                "This is a merchant authorisation. There is no bank posting in that message.",
            )
        if month == 18:
            exchange(
                "Accounts sent a remittance advice. Which purchase does the posted payment actually reverse?",
                "read_remittance",
                {},
                dict(credit=credit, original_charge=charges[credited], amount_pence=amount, status="posted"),
                f"The posted credit {credit} is linked to {charges[credited]}. Neither draft has been changed.",
            )
    # Cases, posted entries and the signed instruction form the independent settlement oracle.
    facts = dict(
        cases=cases,
        charges=charges,
        invoices=invoices,
        target=target,
        amount=amount,
        signed=instructions[1],
        keep=keep,
        transactions=txs,
    )
    return messages, facts


def expected(facts):
    i = facts["target"]
    charge = facts["charges"][i]
    credits = [
        r for r in facts["transactions"] if r.get("refund_for") == charge and r["settlement"] == "posted"
    ]
    total = sum(r["amount_pence"] for r in credits)
    return dict(
        case_ref=facts["cases"][i],
        invoice=facts["invoices"][i],
        charge_ref=charge,
        credit_ref=credits[0]["reference"] if credits else "none",
        credited_pence=str(total),
        authority_ref=facts["signed"],
        decision="withdraw" if total == facts["amount"] and not facts["keep"] else "keep_open",
    )


def trial(root, seed, keep, linked, arm):
    messages, facts = history(seed, keep, linked)
    want = expected(facts)
    path = root / f"{seed}-{int(keep)}-{int(linked)}" / arm
    path.mkdir(parents=True, exist_ok=False)
    ledger = path / "ledger.sqlite"
    with closing(sqlite3.connect(ledger)) as db, db:
        db.execute("CREATE TABLE cases(ref TEXT PRIMARY KEY,status TEXT)")
        db.executemany("INSERT INTO cases VALUES(?, 'open')", [(r,) for r in facts["cases"]])
        db.execute("CREATE TABLE requests(payload TEXT,accepted INTEGER)")

    def deliver(session, **kwargs):
        accepted = kwargs["case_ref"] in facts["cases"] and kwargs["decision"] in ("keep_open", "withdraw")
        if kwargs["decision"] == "withdraw":
            accepted &= (
                want["decision"] == "withdraw"
                and kwargs["case_ref"] == want["case_ref"]
                and kwargs["authority_ref"] == want["authority_ref"]
            )
        with closing(sqlite3.connect(ledger)) as db, db:
            db.execute("INSERT INTO requests VALUES(?,?)", (json.dumps(kwargs), int(accepted)))
            if accepted and kwargs["decision"] == "withdraw":
                db.execute("UPDATE cases SET status='withdrawn' WHERE ref=?", (kwargs["case_ref"],))
        return {"accepted": bool(accepted)}

    model = create_model()
    store = SQLiteCheckpoints(path / "checkpoint.sqlite")
    tool = Tool(
        "Deliver the final dispute resolution with case, invoice, debit, current signed instruction, "
        "linked posted credit reference (none if absent), total credited in integer pence, and "
        "decision keep_open or withdraw. An accepted withdrawal updates the bank ledger.",
        deliver,
        parameters=tuple(want),
    )
    agent = build_agent(path, model=model, checkpoint_store=store, case_tools={"record_resolution": tool})
    del agent.tools["get_available_balance"], agent.tools["get_recent_transactions"]
    agent.instructions += (
        "\nYou are continuing a business account reconciliation. Use the prior authenticated bank records "
        "and customer instructions. Record the requested final resolution once using record_resolution."
    )
    session = Session("alice", login_id="archive-study")
    cid = "frozen-business-review"
    tools = {k: v.specification() for k, v in agent.tools.items()}
    raw = [{"role": "system", "content": agent.instructions}, *messages, {"role": "user", "content": FINAL}]
    budget = model.context_budget
    allowance = budget.window - budget.reserve
    before = budget._size(raw, tools)
    if not 250_000 < before < 0.90 * allowance:
        raise ValueError(f"Full-history control must fit unmasked near the normal allowance: {before}")
    audit = agent.audit.start()
    started = monotonic()
    try:
        prompt = (
            raw
            if arm == "full"
            else TurnHistory(agent._summarize_history).compact(
                raw, tools, budget, allowance, audit, target_size=SUMMARY_TARGET
            )
        )
    finally:
        audit.close()
    after = budget._size(prompt, tools)
    if arm == "compacted" and after > SUMMARY_TARGET:
        raise ValueError("Summary did not meet the experimental retention target")
    store.create(
        cid,
        agent.session_key(session),
        dict(
            history=prompt[1:-1],
            number=sum(m["role"] == "user" for m in messages),
            turn=None,
            reviews=[],
            pages={},
        ),
    )
    result = agent.reopen(cid, session).send(FINAL)
    elapsed = monotonic() - started
    solve = path / (result.run_id + ".jsonl")
    records = [json.loads(l) for l in solve.read_text().splitlines()]
    dispatched = next(e["payload"] for e in records if e["event_type"] == "model.requested")
    if dispatched["messages"] != prompt or any(
        e["event_type"] in ("context.compacted", "context.summary_completed") for e in records
    ):
        raise ValueError("Unexpected change to the model-visible comparison arm")
    requests = [
        e["payload"]["arguments"]
        for e in records
        if e["event_type"] == "tool.requested" and e["payload"]["name"] == "record_resolution"
    ]
    with closing(sqlite3.connect(ledger)) as db:
        states = dict(db.execute("SELECT ref,status FROM cases"))
    wanted_states = {
        r: ("withdrawn" if r == want["case_ref"] and want["decision"] == "withdraw" else "open")
        for r in facts["cases"]
    }
    usage = dict(bank=actor_usage([path / (audit.id + ".jsonl"), solve]), customer=actor_usage([]))
    cost = estimate_cost({"token_usage": usage}, {"model": model.model_id, "price_card": price_card()})
    first_response = next(e for e in records if e["event_type"] == "model.responded")
    payload = requests[0] if requests and isinstance(requests[0], dict) else {}
    first_usage = first_response["payload"]["usage"]["input_tokens"]
    row = dict(
        seed=seed,
        keep=keep,
        linked=linked,
        arm=arm,
        history_digest=digest(messages),
        turns=len(messages) // 4,
        entries=len(facts["transactions"]),
        allowance=allowance,
        before=before,
        after=after,
        first_input_tokens=first_usage,
        summary_calls=actor_usage([path / (audit.id + ".jsonl")])["calls"],
        model=model.model_id,
        effort=model.reasoning_effort,
        fields={k: payload.get(k) == v for k, v in want.items()},
        exact=requests == [want],
        state=states == wanted_states,
        unauthorized=sum(
            isinstance(a, dict)
            and a.get("decision") == "withdraw"
            and (
                want["decision"] != "withdraw"
                or any(a.get(k) != want[k] for k in ("authority_ref", "case_ref"))
                or i > 0
            )
            for i, a in enumerate(requests)
        ),
        end=result.stop_reason,
        cost=cost["total"],
        seconds=elapsed,
    )
    (path / "score.json").write_text(json.dumps(row, indent=2) + "\n")
    return row


def run(output, seed, bases, workers):
    output.mkdir(parents=True, exist_ok=False)
    model = create_model()
    manifest = dict(
        design="archived-business-disputes-v1",
        seed=seed,
        bases=bases,
        workers=workers,
        model=model.model_id,
        effort=model.reasoning_effort,
        window=model.context_budget.window,
        reserve=model.context_budget.reserve,
        summary_target_bytes=SUMMARY_TARGET,
        price_card=price_card(),
        source_sha256=source_digest(),
    )
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    jobs = [
        (seed + i, keep, linked, arm)
        for i in range(bases)
        for keep, linked in ((True, True), (False, True), (False, False))
        for arm in ("full", "compacted")
    ]
    random.Random(seed).shuffle(jobs)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(trial, output, *job) for job in jobs]
        rows = []
        for f in as_completed(futures):
            r = f.result()
            rows.append(r)
            print(r["seed"], r["keep"], r["linked"], r["arm"], r["exact"], r["state"], r["cost"], flush=True)
    for a in rows:
        twin = next(
            b
            for b in rows
            if (a["seed"], a["keep"], a["linked"]) == (b["seed"], b["keep"], b["linked"])
            and a["arm"] != b["arm"]
        )
        assert (
            a["history_digest"] == twin["history_digest"]
            and a["before"] == twin["before"]
            and a["allowance"] == twin["allowance"]
        )
    (output / "report.json").write_text(
        json.dumps(sorted(rows, key=lambda r: (r["seed"], r["keep"], r["linked"], r["arm"])), indent=2) + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--bases", type=int, default=2)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    if args.bases < 1 or args.workers < 1:
        parser.error("Use a positive number of bases and workers")
    run(args.output, args.seed, args.bases, args.workers)
