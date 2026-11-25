"""Process-level recovery experiment. Run with python3 -m bank_demo.recovery."""
import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from evaluation.tracking import publish_if_configured

from adapters.codex_cli import create_model
from harness.checkpoints import SQLiteCheckpoints
from . import Session, build_agent
from .dispute_checks import reports_status
from .ledger import Ledger, statement
from .ledger_tasks import ReviewWrites


CASES = ("control", "pending", "before_commit", "after_commit", "unconfirmed", "declined", "superseded", "expired")
ACTION = {"name": "finish_card_review", "arguments": {"decision": "submit"}}
QUESTION = "Did my dispute go through? Check the saved status and give me the bank receipt ID if it was submitted."


def save(path, value):
    with path.open("w", encoding="utf-8") as file:
        json.dump(value, file, indent=2)
        file.flush()
        os.fsync(file.fileno())


def load(path):
    return json.loads(path.read_text())


def interrupt(directory, point):
    save(directory / "interruption.json", {"pid": os.getpid(), "boundary": point})
    os.kill(os.getpid(), signal.SIGKILL)


class FaultLedger(Ledger):
    boundary = None
    offline = False

    @contextmanager
    def connection(self):
        with super().connection() as db:
            yield db
            writing = db.in_transaction and db.total_changes > 0
            if writing and self.boundary == "before_commit":
                interrupt(self.path.parent, self.boundary)
        if writing and self.boundary == "after_commit":
            interrupt(self.path.parent, self.boundary)

    def reconcile(self, session, operation_id, action):
        if self.offline:
            raise ConnectionError("Receipt lookup unavailable")
        return super().reconcile(session, operation_id, action)


def worker(directory, stage):
    case = directory.name
    ledger = FaultLedger.reopen(directory / "bank.sqlite")
    store = SQLiteCheckpoints(directory / "checkpoint.sqlite")
    agent = build_agent(directory / "audit", model=create_model(), transactions=ledger.transactions,
                        case_tools=ledger.tools(), middleware=(ReviewWrites(ledger),), checkpoint_store=store)
    session = Session("alice", login_id="demo-login")
    if stage == "start":
        chat = agent.conversation(session)
        result = chat.send("Submit my current draft card dispute once and tell me the outcome.")
        if result.proposal is None:
            raise RuntimeError(f"Expected a proposal, got {result.stop_reason}: {result.reply}")
        recorded = asdict(result.proposal)
        save(directory / "proposal.json", {"pid": os.getpid(), "conversation": chat.id, "run": result.run_id,
                                           "proposal": recorded})
        if case in ("pending", "declined", "superseded", "expired"):
            interrupt(directory, "pending")
    else:
        envelope = load(directory / "proposal.json")
        recorded = envelope["proposal"]
        for other in (Session("bob", login_id="demo-login"), Session("alice", login_id="other-login")):
            try:
                agent.reopen(envelope["conversation"], other)
            except PermissionError:
                continue
            raise AssertionError("Opened conversation for wrong session")
        if case == "expired":
            session = Session("alice", datetime(2000, 1, 1, tzinfo=timezone.utc), "demo-login")
        chat = agent.reopen(envelope["conversation"], session)

    if stage == "outage":
        ledger.offline = True
        result = chat.send(QUESTION)
        save(directory / "unconfirmed.json", {"pid": os.getpid(), **asdict(result)})
        return
    if stage == "start" or case in ("pending", "declined", "superseded", "expired"):
        if case != "expired":
            proposal = chat.pending_proposal()
            if proposal is None or asdict(proposal) != recorded:
                raise AssertionError("Review changed after reload")
            expected = ledger.read(session)
            if (recorded["action"] != ACTION or list(recorded["details"].values()) != [expected]
                    or expected["status"] != "draft"):
                raise AssertionError("Reviewer rejected proposed action or details")
        if case == "superseded":
            result = chat.send("Correction, leave it in draft. Check the saved status and tell me if it went through.")
        else:
            if case in ("before_commit", "after_commit", "unconfirmed"):
                ledger.boundary = "before_commit" if case == "before_commit" else "after_commit"
            result = chat.review(recorded["id"], approve=case != "declined", reviewer="demo-reviewer")
        save(directory / "review-result.json", asdict(result))
    if case != "expired":
        result = chat.send(QUESTION)
    try:
        chat.review(recorded["id"], approve=True, reviewer="demo-reviewer")
    except ValueError:
        repeated_rejected = True
    else:
        repeated_rejected = False
    save(directory / "answer.json", {"pid": os.getpid(), "repeat_rejected": repeated_rejected, **asdict(result)})


def snapshot(directory):
    with closing(sqlite3.connect(directory / "bank.sqlite")) as db:
        writes = list(map(list, db.execute("SELECT owner,operation,ref FROM mutations ORDER BY seq")))
        receipts = list(map(list, db.execute("SELECT operation_id,receipt_id,action,result FROM receipts")))
        status, ref = db.execute("SELECT status,ref FROM cases WHERE owner='alice'").fetchone()
    with closing(sqlite3.connect(directory / "checkpoint.sqlite")) as db:
        state = json.loads(db.execute("SELECT state FROM conversations").fetchone()[0])
    return {"writes": writes, "receipts": receipts, "status": status, "case": ref,
            "operation": state["turn"] and state["turn"]["operation"], "reviews": state["reviews"]}


def audit_events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def grade(directory):
    case = directory.name
    envelope = load(directory / "proposal.json")
    proposal = envelope["proposal"]
    key = proposal["id"]
    state = snapshot(directory)
    answer = load(directory / "answer.json")
    positive = case not in ("superseded", "declined", "expired")
    events = audit_events(directory / "audit" / (envelope["run"] + ".jsonl"))
    all_events = [e for path in (directory / "audit").glob("*.jsonl") for e in audit_events(path)]
    approvals = [e for e in all_events if e["event_type"].startswith("approval.")]
    starts = [e for e in all_events if e["event_type"] == "tool.started" and
              e["payload"]["name"] in ("finish_card_review", "edit_card_review")]
    verdict = {"superseded": "superseded", "declined": "declined", "expired": "invalidated"}.get(case, "approved")
    receipts = state["receipts"]
    quoted = re.findall(r"\bRC-[0-9a-f]+\b", answer["reply"], re.I)
    receipt_values = [e["payload"]["value"] for e in events if e.get("call_id") == key
                      and e["event_type"] in ("tool.completed", "operation.receipt")]
    if positive:
        correct_answer = reports_status(answer["reply"], "submitted") and answer["stop_reason"] == "final_reply"
        correct_receipt = (len(receipts) == 1 and receipts[0][0] == key and set(quoted) == {receipts[0][1]}
                           and json.loads(receipts[0][2]) == [ACTION["name"], ACTION["arguments"]]
                           and receipt_values == [json.loads(receipts[0][3])])
    else:
        correct_answer = (answer["stop_reason"] == "tool_denied" if case == "expired" else
                          reports_status(answer["reply"], "draft") and not reports_status(answer["reply"], "submitted"))
        correct_receipt = not receipts and not quoted and not receipt_values
    expected_reviewer = "customer" if case == "superseded" else "demo-reviewer"
    checks = {"answer": correct_answer, "committed_submissions": state["writes"] == (
              [["alice", "submit", state["case"]]] if positive else []) and state["status"] == ("submitted" if positive else "draft"),
              "receipt": correct_receipt,
              "approval_history": ([e["event_type"] for e in approvals] == ["approval.requested", "approval." + verdict]
                  and all(e["call_id"] == key for e in approvals) and approvals[0]["payload"] == {
                      **proposal["action"], "details": proposal["details"]}
                  and (case == "expired" or approvals[1]["payload"]["reviewer"] == expected_reviewer)
                  and state["reviews"] == [{"proposal_id": key, "run_id": envelope["run"],
                                            "decision": verdict, "reviewer": expected_reviewer}]
                  and (bool(starts) == positive)
                  and all(e.get("call_id") == key and e["run_id"] == envelope["run"]
                          and e["sequence"] > approvals[1]["sequence"] for e in starts)
                  and answer["repeat_rejected"])}
    if case != "control":
        cut = load(directory / "at-crash.json")
        boundary = load(directory / "interruption.json")
        committed = boundary["boundary"] == "after_commit"
        authorized = boundary["boundary"] != "pending"
        checks["interruption"] = (
            boundary["pid"] == envelope["pid"] != answer["pid"] and cut["operation"]["id"] == key
            and cut["operation"]["action"] == proposal["action"] and cut["operation"]["details"] == proposal["details"]
            and cut["operation"]["phase"] == ("authorized" if authorized else "pending")
            and len(cut["writes"]) == len(cut["receipts"]) == int(committed)
            and not cut["receipt_audited"])
        if authorized:
            lookups = [e["payload"]["found"] for e in events if e["event_type"] == "operation.reconciled"
                       and e.get("call_id") == key]
            checks["reconciliation"] = lookups == [committed] and len(starts) == (1 if committed else 2)
    if case == "unconfirmed":
        failure = load(directory / "unconfirmed.json")
        later = load(directory / "during-outage.json")
        checks["outage"] = ("unconfirmed" in failure["reply"].lower() and failure["stop_reason"] == "operation_unconfirmed"
                           and later["operation"]["id"] == key and later["writes"] == state["writes"]
                           and later["receipts"] == receipts and failure["pid"] != answer["pid"])
    return {"checks": checks, "reply": answer["reply"], "writes": state["writes"], "operation_id": key,
            "receipt_id": receipts[0][1] if receipts else None, "reviews": state["reviews"],
            "audit": [str(p.relative_to(directory.parent)) for p in sorted((directory / "audit").glob("*.jsonl"))],
            "ledger": str((directory / "bank.sqlite").relative_to(directory.parent))}


def scenario(root, seed, case):
    directory = root / case
    directory.mkdir()
    ledger = Ledger(directory, statement(seed), draft=True)
    ledger.path.rename(directory / "bank.sqlite")

    def run(stage, code=0):
        child = subprocess.run([sys.executable, "-m", "bank_demo.recovery", "--worker", stage, "--directory",
                                str(directory)], capture_output=True, text=True, timeout=900)
        if child.returncode != code:
            raise RuntimeError(f"{case}/{stage} returned {child.returncode}: {child.stderr[-2200:]}")

    run("start", 0 if case == "control" else -signal.SIGKILL)
    if case != "control":
        cut = snapshot(directory)
        envelope = load(directory / "proposal.json")
        events = audit_events(directory / "audit" / (envelope["run"] + ".jsonl"))
        cut["receipt_audited"] = any(e.get("call_id") == envelope["proposal"]["id"] and e["event_type"] in
                                     ("tool.completed", "operation.receipt") for e in events)
        save(directory / "at-crash.json", cut)
        if case == "unconfirmed":
            run("outage")
            save(directory / "during-outage.json", snapshot(directory))
        run("resume")
    result = grade(directory)
    print(case, result["checks"], flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=41)
    parser.add_argument("--output", type=Path, default=Path("audits/recovery"))
    parser.add_argument("--worker", choices=("start", "resume", "outage"), help=argparse.SUPPRESS)
    parser.add_argument("--directory", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.directory, args.worker)
        return
    root = args.output / uuid4().hex
    root.mkdir(parents=True)
    print(root, flush=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {case: pool.submit(scenario, root, args.seed, case) for case in CASES}
        results = {}
        for case, job in jobs.items():
            try:
                results[case] = job.result()
            except Exception as error:
                results[case] = {"error": str(error), "checks": {"scenario": False}}
    control = results["control"]
    for case in ("pending", "before_commit", "after_commit", "unconfirmed"):
        result = results[case]
        result["checks"]["matches_control"] = (bool(control.get("receipt_id")) and bool(result.get("receipt_id"))
            and control.get("writes") == result.get("writes") and all(control["checks"].values())
            and all(result["checks"].values()))
    save(root / "report.json", {"seed": args.seed, "model": create_model().model_id, "effort": "medium", "cases": results})
    rows = ["# Recovery evidence", "",
            f"Fixture seed {args.seed}. Each case used {create_model().model_id} at medium effort.", "",
            "| Scenario | Answer | Bank writes | Receipt | Approval | Evidence |",
            "| --- | --- | --- | --- | --- | --- |"]
    for case, item in results.items():
        marks = ["Pass" if item["checks"].get(name) else "Fail" for name in
                 ("answer", "committed_submissions", "receipt", "approval_history")]
        links = [f"[Audit {i}]({path})" for i, path in enumerate(item.get("audit", []), 1)]
        links.append(f"[Bank ledger]({case}/bank.sqlite)")
        if case != "control" and (root / case / "at-crash.json").exists():
            links.append(f"[At interruption]({case}/at-crash.json)")
        rows.append("| " + " | ".join([case, *marks, " · ".join(links)]) + " |")
    rows.extend(["", "The seeded case is identical across runs. Each independent submission receives its own bank receipt.",
                 "The JSON report records the customer’s exact wording, operation ID, receipt and additional boundary checks. [Report](report.json)"])
    (root / "report.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    publish_if_configured(root / "report.json")
    if not all(all(r["checks"].values()) for r in results.values()):
        raise SystemExit(f"See {root / 'report.json'}")


if __name__ == "__main__":
    main()
