import json
import re
import sqlite3
from contextlib import closing
from copy import deepcopy
from pathlib import Path

from evaluation.casebooks import digest, trial_directory
from .instances import validate


def freeze_handoff(directory, case_id=None, repeat=1):
    directory = Path(directory)
    report = directory / "report.json" if directory.is_dir() else directory
    directory = report.parent
    book = json.loads((directory / "casebook.json").read_text())
    validate(book)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("casebook_id") != digest(book):
        raise ValueError("Casebook differs from source manifest")
    rows = json.loads(report.read_text())
    matches = [r for r in rows if r["task"] == "handoff_retain_draft" and r["repeat"] == repeat
               and (case_id is None or r["case_id"] == case_id)]
    if len(matches) != 1 or matches[0]["status"] != "PASS":
        raise ValueError("Select one successfully completed keep-open handoff by case ID and repeat")
    row = matches[0]
    case = deepcopy(next(c for c in book["cases"] if c["case_id"] == row["case_id"]))
    ledger_names = {c["evidence"].split(":", 1)[0] for c in row["checks"]
                    if c["criterion"].endswith("Exact committed ledger writes")}
    if len(ledger_names) != 1 or not re.fullmatch(r"ledger-[a-f0-9]+\.sqlite", next(iter(ledger_names))):
        raise ValueError("Missing source ledger identity")
    path = trial_directory(directory, row) / ledger_names.pop()
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        expected_case = case["world"]["statement"]["case"]
        saved = db.execute("SELECT ref,status FROM cases WHERE owner='alice'").fetchone()
        if saved != (expected_case, "draft") or db.execute("SELECT count(*) FROM mutations").fetchone()[0]:
            raise ValueError("The source must still have its original open draft")
        threads = case["evidence"]["threads"]
        for thread in threads:
            history = db.execute("SELECT dialogue,updated_at FROM conversations WHERE owner=? AND id=?",
                                 (thread["owner"], thread["id"])).fetchone()
            if history is None:
                raise ValueError("Missing archived conversation")
            thread["dialogue"], thread["updated_at"] = json.loads(history[0]), history[1]
            for customer, reply in db.execute("SELECT customer,reply FROM continuation WHERE thread_id=? ORDER BY rowid",
                                              (thread["id"],)):
                thread["dialogue"].extend([["customer", customer], ["adviser", reply]])
    prior = next(t for t in threads if t["id"] == case["evidence"]["prior_id"])
    expected = [["customer" if turn["speaker"] == "customer" else "adviser", turn["text"]] for turn in row["dialogue"]]
    if not expected or prior["dialogue"][-len(expected):] != expected:
        raise ValueError("Saved thread disagrees with the source run's public dialogue")
    if "previous_reply" not in case:
        case["messages"][0] += " Quote the previous adviser's last reply about the saved case verbatim for my notes."
    case["previous_reply"] = expected[-1][1]
    case["source"] = {"batch": directory.name, "case_id": row["case_id"], "repeat": repeat,
                      "last_turn": Path(row["turn_audits"][-1]).stem}
    case.pop("case_id")
    updated = {**book, "cases": [{**case, "case_id": digest([book["version"], case])}]}
    validate(updated)
    return updated
