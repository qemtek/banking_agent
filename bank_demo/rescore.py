import argparse
import json
import re
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from evaluation import Check, Scenario, Turn, copy_database, read_turn, scores
from evaluation.casebooks import digest, trial_directory
from evaluation.scoring import SCORE_VERSION
from harness import Result

from .ledger_tasks import claim_tasks, ledger_tasks
from .instances import materialize, validate
from .tasks import bank_tasks


TURN_SNAPSHOT_VERSION = 5


def saved_source(check):
    if "source" in check:
        return check["source"]
    name = check["criterion"].split(" · ")[-1]
    if name in ("Only the authorized decision reaches review", "Only the corrected action reaches review",
                "Proposal before reviewer decision", "Ledger untouched before reviewer decision",
                "Submitted proposal fits the policy decision", "Correction invalidated the earlier approval",
                "No write proposed"):
        return "review"
    if check["category"] in ("bank_writes", "handoff") and name != "Recovers the outgoing agent's actual final reply":
        return "state"
    if name == "Correct specialist queue, priority and evidence":
        return "state"
    return "turn"


def read_saved_turn(path, conversation, number, *, renamed_opening=False):
    with path.open() as stream:
        first = json.loads(next(stream))
        last = first
        for line in stream:
            last = json.loads(line)
    run_id = first["run_id"] if renamed_opening else path.stem
    result = Result(run_id, last["payload"]["reply"], last["payload"]["reason"])
    return result, read_turn(path, result, conversation or first["payload"]["conversation_id"], number)


def replay_row(task, row, directory, version):
    if row["status"] == "ERROR":
        return {**row, "dimensions": scores([], error=True)[1], "score_version": SCORE_VERSION}
    turns = (Turn(task.message, task.score), *task.followups)
    if len(row["turn_audits"]) != len(turns):
        raise ValueError("Incomplete turn evidence")
    trial = trial_directory(directory, row)
    stored = [Check(**{**c, "source": saved_source(c)}) for c in row["checks"]]
    checks = [c for c in stored if c.source == "review"]
    state = [c for c in stored if c.source == "state"]
    conversation = None
    dialogue = []
    origins = []
    history = []
    with TemporaryDirectory() as scratch:
        fixture = task.setup(Path(scratch))
        database = fixture.state_database if isinstance(fixture, Scenario) else None
        ledger_names = {m[0] for c in state if (m := re.match(r"ledger-[a-f0-9]+\.sqlite", c.evidence))}
        for n, (turn, audit) in enumerate(zip(turns, row["turn_audits"]), 1):
            path = trial / Path(audit).name
            result, events = read_saved_turn(path, conversation, n)
            conversation = events[0]["payload"]["conversation_id"]
            for index, event in enumerate(events):
                if event["event_type"] == "approval.superseded":
                    events = events[:index]
                    result = Result(path.stem, "This action is awaiting review.", "awaiting_approval")
                    break
            if task.render_reply:
                result = task.render_reply(result, events)
            dialogue.extend([{"speaker": "customer", "text": events[0]["payload"]["message"]},
                             {"speaker": "bank", "text": result.reply}])
            prefix = f"Turn {n} · " if len(turns) > 1 else ""
            checks.extend(replace(c, criterion=prefix + c.criterion) for c in task.score_turn(turn, result, events, history))
            snapshot = trial / "snapshots" / (path.stem + ".sqlite")
            if database and (snapshot.is_file() or n == len(turns) or version >= TURN_SNAPSHOT_VERSION):
                origin = "turn snapshot"
                if not snapshot.is_file() and version < TURN_SNAPSHOT_VERSION and len(ledger_names) == 1:
                    snapshot = trial / next(iter(ledger_names))
                    origin = "final ledger"
                if not snapshot.is_file():
                    raise ValueError(f"Missing state evidence for turn {n}")
                copy_database(snapshot, database)
                for c in fixture.check_state(n, events[0]["payload"]["message"], result.reply):
                    checks.append(replace(c, criterion=prefix + c.criterion, source="state",
                                          evidence=c.evidence.replace(database.name, next(iter(ledger_names), snapshot.name))))
                origins.append(origin)
            else:
                checks.extend(c for c in state if c.criterion.startswith(prefix))
                if state:
                    origins.append("recorded state checks")
    if row.get("dialogue", dialogue) != dialogue:
        raise ValueError("Public dialogue differs from the turn audits")
    status, dimensions = scores(checks)
    return {**row, "status": status, "dimensions": dimensions, "checks": [asdict(c) for c in checks],
            "dialogue": dialogue, "original_status": row["status"], "score_version": SCORE_VERSION,
            "state_evidence": origins}


def rescore(directory):
    from .casebook import source_digest
    replay_source = source_digest()
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    factory = lambda: None  # Rebuild the fixture in scratch space. Scoring never calls a model.
    tasks = list(bank_tasks(factory))
    generated = {}
    if manifest.get("suite") == "generated":
        book = json.loads((directory / "casebook.json").read_text())
        validate(book)
        if digest(book) != manifest["casebook_id"]:
            raise ValueError("Casebook differs from the execution manifest")
        generated = {case["case_id"]: case for case in book["cases"]}
    elif "seed" in manifest:
        environment = manifest.get("ledger_environment", "original")
        tasks.extend(ledger_tasks(factory, manifest["seed"], environment))
        tasks.extend(claim_tasks(factory, manifest["seed"], environment))
    tasks = {task.name: task for task in tasks}
    report = []
    for row in json.loads((directory / "report.json").read_text()):
        task = materialize(generated[row["case_id"]], factory,
                           card_selection_version=manifest.get("card_selection_version", 1)) if generated else tasks[row["task"]]
        replayed = replay_row(task, row, directory, manifest.get("score_version", 1))
        report.append({**replayed, "replay_source_sha256": replay_source})
    return report


def main():
    parser = argparse.ArgumentParser(description="Regrade saved conversations without running the bank or customer.")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    rows = rescore(args.directory)
    output = args.directory / f"rescore-v{SCORE_VERSION}.json"
    contents = json.dumps(rows, indent=2) + "\n"
    if output.exists() and output.read_text() != contents:
        parser.error("Saved replay differs. Preserve the existing report and investigate the discrepancy.")
    if not output.exists():
        with output.open("x") as target:
            target.write(contents)
    for key in ("operational", "communication"):
        print(key, dict(Counter(r["dimensions"][key] for r in rows)))
    print(output)


if __name__ == "__main__":
    main()
