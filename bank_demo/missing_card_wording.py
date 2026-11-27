"""Compare two return messages from the same saved invitation and signed bank state."""
import argparse
import asyncio
import json
import random
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from shutil import copyfile

from adapters.codex_cli import create_model
from evaluation import Outcome, copy_database, read_turn, scores
from evaluation.casebooks import digest, trial_directory
from evaluation.usage import estimate_cost, price_card, usage_scorecard
from harness.checkpoints import SQLiteCheckpoints
from .benchflow_trial import execute, preflight
from .casebook import source_digest
from .instances import materialize, validate


MESSAGES = {"neutral": "Any update?", "explicit": "I’ve signed in the app. Can you check again?"}


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def write(path, value):
    staging = path.with_suffix(".tmp")
    staging.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    staging.replace(path)


def score_return(case, directory, factory):
    message = case["return_message"]
    original = json.loads((directory / "request.json").read_text())
    first = events(directory / "opening.jsonl")
    model = factory()
    task = materialize(case, lambda: model)
    fixture = task.setup(directory)
    copy_database(directory / "opening.sqlite", fixture.state_database)
    fixture.before_turn(2)
    w = case["world"]
    signed = next(e for e in w["app"] if e["turn"] == 2)
    with closing(sqlite3.connect(fixture.state_database)) as db:
        evidence = db.execute("SELECT instruction_id,status,destination FROM signatures WHERE turn=2 AND attempt=?",
                              (w["attempt"],)).fetchall()
        if evidence != [(signed["id"], "signed", signed["destination"])]:
            raise ValueError("Return fork lacks the scheduled signed instruction")
        state_hash = digest(list(db.iterdump()))

    # Restore the unabridged model-visible conversation, including the original tool call IDs.
    agent = fixture.agent
    agent.instructions = original["messages"][0]["content"]
    agent.checkpoints = store = SQLiteCheckpoints(directory / "checkpoint.sqlite")
    cid = first[0]["payload"]["conversation_id"]
    session = replace(fixture.session, login_id=cid)
    store.create(cid, agent.session_key(session),
                 {"history": original["messages"][1:-1], "number": 1, "turn": None, "reviews": [], "pages": {}})
    result = agent.reopen(cid, session).send(message)
    audit = directory / (result.run_id + ".jsonl")
    records = read_turn(audit, result, cid, 2)
    request = next(e["payload"] for e in records if e["event_type"] == "model.requested")
    wanted = {**original, "messages": [*original["messages"][:-1], {"role": "user", "content": message}]}
    if request != wanted:
        raise ValueError("Return request differs from the original beyond the customer message")
    result = task.render_reply(result, records)
    checks = [*task.score_turn(task.followups[0], result, records, [first]), *fixture.score_state(2)]
    error = result.stop_reason if result.stop_reason in ("model_error", "context_error", "middleware_error") else ""
    status, dimensions = scores(checks, error=bool(error))
    row = usage_scorecard(Outcome(task.name, status, str(audit), checks, error, [str(audit)],
                                  [{"speaker": "customer", "text": message}, {"speaker": "bank", "text": result.reply}],
                                  dimensions), None)
    with closing(sqlite3.connect(fixture.state_database)) as db:
        order = db.execute("SELECT instruction_id,destination,receipt FROM orders WHERE attempt=? AND origin='replacement'",
                           (w["attempt"],)).fetchall()
    issued = order == [(signed["id"], signed["destination"], w["receipts"]["replacement"])]
    reread = any(e["event_type"] == "tool.completed" and e["component"] == "read_card_instruction"
                 and (e["payload"]["value"].get("current") or {}).get("id") == signed["id"] for e in records)
    return {**row, "read_signed_instruction": reread, "issued": issued, "endpoint": reread and issued,
            "opening_state_sha256": state_hash, "prior_prompt_and_tools_sha256": digest(original)}


def freeze_inputs(source, output, *, stalled_returns=False):
    book = json.loads((source / "casebook.json").read_text())
    validate(book)
    meta = json.loads((source / "manifest.json").read_text())
    if (book["split"] != "development" or (not stalled_returns and book.get("design") != "missing-card-paired-development-v1")
            or digest(book) != meta["casebook_id"] or meta["extra_instruction"] or meta["repeats"] != 1):
        raise ValueError("Expected the standard missing-card development pilot")
    cases = {c["case_id"]: c for c in book["cases"] if len(c["messages"]) == 2
             and c["family"] in {"missing_card_contact", "missing_card_continuation"} and c["variant"] == "final_failure"}
    report = json.loads((source / "report.json").read_text())
    if {r["case_id"] for r in report} != {c["case_id"] for c in book["cases"]} or len(report) != len(book["cases"]):
        raise ValueError("Source report does not cover the frozen casebook exactly once")
    inputs = {}
    for row in report:
        cid = row["case_id"]
        if cid not in cases:
            continue
        trial = trial_directory(source, row)
        a, b = [trial / Path(p).name for p in row["turn_audits"]]
        first = events(a)
        second = events(b)
        if stalled_returns and (row["dimensions"]["operational"] != "FAIL" or
                                any(e["event_type"] == "tool.requested" for e in second)):
            continue
        request = next(e["payload"] for e in second if e["event_type"] == "model.requested")
        if (request["messages"][-1] != {"role": "user", "content": cases[cid]["messages"][1]}
                or not any(e["event_type"] == "tool.completed" and e["component"] == "invite_card_instruction"
                           and "error" not in e["payload"]["value"] for e in first)):
            raise ValueError("Expected a saved invitation followed by the recorded return")
        location = output / "inputs" / cid
        location.mkdir(parents=True)
        copy_database(trial / "snapshots" / (a.stem + ".sqlite"), location / "opening.sqlite")
        copyfile(a, location / "opening.jsonl")
        write(location / "request.json", request)
        inputs[cid] = cases[cid]
    if (not stalled_returns and inputs.keys() != cases.keys()) or not inputs:
        raise ValueError("Incomplete source pilot")
    return inputs, meta


def run(source, output, repeats, workers, *, stalled_returns=False):
    if min(repeats, workers) < 1:
        raise ValueError("Workers and repeats must be positive")
    runtime = preflight()
    output.mkdir(parents=True, exist_ok=False)
    cases, origin = freeze_inputs(source, output, stalled_returns=stalled_returns)
    messages = {cid: {"original": case["messages"][1], "neutral": MESSAGES["neutral"]} if stalled_returns else MESSAGES
                for cid, case in cases.items()}
    schedule = [(cid, n, wording) for cid in sorted(cases) for n in range(1, repeats + 1) for wording in messages[cid]]
    random.Random(26112683).shuffle(schedule)
    model = create_model()
    manifest = {"source_batch": source.name, "casebook_id": origin["casebook_id"], "model": model.model_id,
                "reasoning_effort": model.reasoning_effort, "price_card": price_card(), **runtime,
                "source_sha256": source_digest(), "messages_by_case": messages, "schedule": schedule,
                "endpoint": "Fresh read of the turn-two signature and exactly one saved replacement against it",
                "repeats": repeats, "workers": workers}
    if not stalled_returns:
        manifest["messages"] = MESSAGES
    write(output / "manifest.json", manifest)

    def trial(cid, n, wording):
        case = cases[cid]
        directory = output / "trials" / cid / str(n) / wording
        directory.mkdir(parents=True)
        for name in ("opening.sqlite", "opening.jsonl", "request.json"):
            copyfile(output / "inputs" / cid / name, directory / name)
        row = asyncio.run(execute({**case, "return_message": messages[cid][wording]}, directory,
                                  "scripted", "", frozen_card_return=True))
        return {**row, "estimated_cost_usd": estimate_cost(row, manifest)}

    rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(trial, *job): job for job in schedule}
        for future in as_completed(pending):
            cid, n, wording = pending[future]
            try:
                row = future.result()
            except Exception as exc:
                row = {"status": "ERROR", "error": str(exc)}
            case = cases[cid]
            rows.append({**row, "case_id": cid, "base_world_id": case.get("base_world_id", cid),
                         "sibling": case.get("sibling"), "repeat": n, "wording": wording})
            write(output / "report.json", rows)
    print(dict(Counter((r["wording"], r.get("endpoint"), r["status"]) for r in rows)))
    return int(any(r["status"] == "ERROR" for r in rows))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--stalled-returns", action="store_true",
                        help="Compare the original wording with Any update? on failed no-tool returns")
    args = parser.parse_args()
    raise SystemExit(run(args.source.resolve(), args.output.resolve(), args.repeats, args.workers,
                         stalled_returns=args.stalled_returns))
