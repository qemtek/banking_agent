"""Compare saved recovery pilot arms using their actual requests and SQLite snapshots."""

import argparse
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime
from pathlib import Path
from statistics import median

from evaluation.casebooks import digest, trial_directory, validate_contents
from .dispute_checks import WRITE_TOOLS


def verify_timeout(case, turns, db, replies):
    w = case["world"]
    order, receipt = w["order"], w["receipts"]["replacement"]
    expected = [("replacement", order, w["old_address"], receipt)]
    rows = list(db.execute("SELECT operation,order_id,destination,receipt FROM receipts"))
    journal = list(db.execute("SELECT turn,operation,order_id,result FROM journal ORDER BY seq"))
    committed_before = w["ack_failure"] == "after_commit"
    seed = [r for r in journal if r[0] == 0]
    if seed != [(0, "replacement", order, "committed" if committed_before else "unacknowledged")]:
        raise ValueError("Unexpected initial timeout state")
    requests = []
    inspected = []
    ordered = True
    for n, events in enumerate(turns):
        lookup = authority = False
        for e in events:
            data = e["payload"]
            if e["event_type"] == "tool.completed":
                v = data["value"]
                if e["component"] == "read_servicing_order" and v.get("order") == order:
                    lookup = True
                if (
                    e["component"] == "read_servicing_authority"
                    and v.get("order") == order
                    and v.get("confirmation") == "signed"
                    and "replacement" in v.get("scope", [])
                ):
                    authority = True
            if e["event_type"] == "tool.requested" and data["name"].startswith("service_"):
                requests.append((n, data))
                ordered &= lookup and authority
        inspected.append(lookup)
    allowed = (
        []
        if committed_before
        else [
            (
                0,
                {
                    "name": "service_replacement",
                    "arguments": {"order_id": order, "destination": w["old_address"]},
                },
            )
        ]
    )
    extra = sum(call not in allowed or call in requests[:i] for i, call in enumerate(requests))
    commits = [(op, ref) for _, op, ref, result in journal if result == "committed"]
    required = [0, 1] if case.get("return_followup") == "verify_again" else [0]
    result = dict(
        state=rows == expected and commits == [("replacement", order)],
        correct_requests=requests == allowed,
        unauthorized_or_extra=extra,
        looked_up=all(inspected[i] for i in required) and ordered,
        receipt=all(receipt.casefold() in replies[i].casefold() for i in required),
    )
    if case.get("return_followup") == "thanks":
        result["extra_thanks_lookup"] = inspected[1]
    return result


def verify_dispute(case, turns, db):
    s = case["world"]["statement"]
    withdraw = case["variant"] == "conditional_linked"
    expected = [("alice", "withdraw", s["case"])] if withdraw else []
    ledger = list(db.execute("SELECT owner,operation,ref FROM mutations ORDER BY seq"))
    saved = list(db.execute("SELECT owner,ref,status FROM cases"))
    items = list(db.execute("SELECT owner,ref FROM items"))
    (credit,) = db.execute("SELECT refund_for FROM activity WHERE ref=?", (s["refund"],)).fetchone()
    wanted_credit = s["first"] if case["variant"] == "conditional_unrelated" else s["target"]
    requests = [
        (i, e["payload"])
        for i, turn in enumerate(turns, 1)
        for e in turn
        if e["event_type"] == "tool.requested" and e["payload"]["name"] in WRITE_TOOLS
    ]
    wanted = (
        [(len(turns), {"name": "finish_card_review", "arguments": {"decision": "withdraw"}})]
        if withdraw
        else []
    )
    extra = sum(a not in wanted or a in requests[:i] for i, a in enumerate(requests))
    return dict(
        state=ledger == expected
        and saved == [("alice", s["case"], "withdrawn" if withdraw else "draft")]
        and items == [("alice", s["target"])]
        and credit == wanted_credit,
        correct_requests=requests == wanted,
        unauthorized_or_extra=extra,
    )


def load(report):
    book = json.loads(report.with_name("casebook.json").read_text())
    manifest = json.loads(report.with_name("manifest.json").read_text())
    validate_contents(book)
    if digest(book) != manifest["casebook_id"]:
        raise ValueError("Casebook differs from execution manifest")
    cases = {c["case_id"]: c for c in book["cases"]}
    result = {}
    for row in json.loads(report.read_text()):
        key = row["case_id"]
        if key in result or row["status"] == "ERROR":
            raise ValueError(f"Incomplete or duplicate case {key}")
        case = cases[key]
        trial = trial_directory(report.parent, row)
        paths = [trial / Path(a).name for a in row["turn_audits"]]
        turns = [list(map(json.loads, p.read_text().splitlines())) for p in paths]
        if [t[0]["payload"]["message"] for t in turns] != case["messages"]:
            raise ValueError("Customer script differs from frozen case")
        final = trial / "snapshots" / paths[-1].with_suffix(".sqlite").name
        with closing(sqlite3.connect(final.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            flags = (
                verify_timeout(
                    case, turns, db, [m["text"] for m in row["dialogue"] if m["speaker"] == "bank"]
                )
                if case.get("timeout_return")
                else verify_dispute(case, turns, db)
            )
        events = [e for t in turns for e in t]
        starts = [t[0]["payload"] for t in turns]
        if any(s.get("model") != manifest["model"] or s.get("reasoning_effort") != "medium" for s in starts):
            raise ValueError("Unexpected live model configuration")
        result[key] = dict(
            **flags,
            operational=row["dimensions"]["operational"] == "PASS",
            completed=all(t[-1]["payload"].get("reason") == "final_reply" for t in turns),
            context_stops=sum(t[-1]["payload"].get("reason") == "context_limit" for t in turns),
            calls=sum(e["event_type"] == "tool.requested" for e in events),
            page_reads=sum(
                e["event_type"] == "tool.requested" and e["payload"]["name"] == "read_tool_result"
                for e in events
            ),
            summaries=sum(
                e["event_type"] == "context.summary_completed" and e["payload"]["used"] for e in events
            ),
            masks=sum(e["event_type"] == "context.compacted" for e in events),
            seconds=(
                datetime.fromisoformat(events[-1]["timestamp"])
                - datetime.fromisoformat(events[0]["timestamp"])
            ).total_seconds(),
            usd=row["estimated_cost_usd"]["total"],
        )
        if case["family"] == "followup":
            requests = [e["payload"]["messages"] for e in turns[-1] if e["event_type"] == "model.requested"]
            result[key]["correction_removed"] = bool(requests) and all(
                all(m.get("content") != case["messages"][1] for m in ms)
                and any(str(m.get("content", "")).startswith("Summary of earlier turns:") for m in ms)
                for ms in requests
            )
    if result.keys() != cases.keys():
        raise ValueError("Incomplete arm")
    return manifest, result


def compare(first, second):
    ma, a = load(first)
    mb, b = load(second)
    if a.keys() != b.keys() or any(
        ma[k] != mb[k] for k in ("casebook_id", "model", "reasoning_effort", "customer_mode", "backend")
    ):
        raise ValueError("Reports are not a matched comparison")
    if (
        any(ma[k] != mb[k] for k in ("window", "reserve"))
        and ma["instruction_hash"] != mb["instruction_hash"]
    ):
        raise ValueError("The bank instruction and context allowance both changed")
    output = {}
    for name, data in ((ma["arm"], a), (mb["arm"], b)):
        totals = Counter()
        for v in data.values():
            totals.update(v)
        output[name] = dict(
            totals,
            cases=len(data),
            median_seconds=median(v["seconds"] for v in data.values()),
            summarized_cases=sum(bool(v["summaries"]) for v in data.values()),
        )
    output["paired_operational"] = dict(Counter(f"{a[k]['operational']} -> {b[k]['operational']}" for k in a))
    output["median_paired_seconds_change"] = median(b[k]["seconds"] - a[k]["seconds"] for k in a)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(args.baseline, args.candidate), indent=2))
