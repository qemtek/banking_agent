"""Reproduce the Terra-medium development report from archived standard-bank batches."""
import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from math import ceil
from pathlib import Path
from statistics import median

from evaluation.casebooks import trial_directory
from evaluation.scoring import SCORE_VERSION
from evaluation.uplift import load_arm
from evaluation.usage import report_cost

from .rescore import rescore


BATCHES = {
    "core": ["49e3d97e22f146ecaf440ccb95e183ea"],
    "products": ["7396de73ae5744f9933ced2510a4ebef"],
    "revised_card_selection": ["81d99717699b4d618c14f16619a68824"],
    "servicing": ["74aeaf0a101149cd8112b7c6b59a7b8b", "5dd2495a11d54ecab5dadd92c30f631f"],
    "missing_card": ["650b2483278f41b4aaf11b899d53fe66"],
}


def distribution(values):
    if not values:
        return {}
    ordered = sorted(values)
    return {"n": len(values), "median": median(values), "p95": ordered[ceil(.95 * len(values)) - 1],
            "max": ordered[-1]}


def verdicts(rows):
    return {"attempts": len(rows), "operational": Counter(r["dimensions"]["operational"] for r in rows),
            "communication": Counter(r["dimensions"]["communication"] for r in rows),
            "whole": Counter(r["status"] for r in rows)}


def grouped(rows, field):
    groups = defaultdict(list)
    for row in rows:
        groups[row[field]].append(row)
    return {name: verdicts(group) for name, group in sorted(groups.items())}


def performance(directory, row):
    requests = 0
    durations = []
    for name in row["turn_audits"]:
        path = trial_directory(directory, row) / Path(name).name
        with path.open() as audit:
            events = [json.loads(line) for line in audit]
        if events[0]["event_type"] != "run.started" or events[-1]["event_type"] != "run.finished":
            raise ValueError(f"Incomplete bank turn audit in {path}")
        requests += sum(e["event_type"] == "tool.requested" for e in events)
        start, end = (datetime.fromisoformat(e["timestamp"]) for e in (events[0], events[-1]))
        durations.append((end - start).total_seconds())
    return requests, durations


def summarize(root):
    cohorts = {}
    for cohort, batches in BATCHES.items():
        rows, costs, requests, durations, provenance = [], [], [], [], []
        case_ids = set()
        for batch in batches:
            directory = root / batch
            manifest, archived = load_arm(directory)
            if (manifest["split"], manifest["model"], manifest["reasoning_effort"], manifest["customer_mode"],
                manifest["repeats"], manifest["extra_instruction"]) != ("development", "gpt-6.1-terra", "medium", "llm", 1, ""):
                raise ValueError(f"Unexpected arm settings for {batch}")
            replayed = rescore(directory)
            provenance.append({"batch": batch, "casebook": manifest["casebook_id"], "arm": manifest["arm"],
                               "execution_scorer": manifest["score_version"],
                               "replay_source": replayed[0]["replay_source_sha256"]})
            for row in replayed:
                key = row["case_id"]
                if key in case_ids or (key, 1) not in archived:
                    raise ValueError(f"Unexpected or repeated case {key}")
                case_ids.add(key)
                rows.append(row)
                costs.append(report_cost(row, manifest))
                if row["status"] != "ERROR":
                    tools, times = performance(directory, row)
                    requests.append(tools)
                    durations.extend(times)
        cohorts[cohort] = {
            **verdicts(rows), "by_family": grouped(rows, "family"), "by_difficulty": grouped(rows, "difficulty"),
            "bank_requests_per_conversation": distribution(requests), "bank_seconds_per_turn": distribution(durations),
            "fully_priced": sum("total" in cost for cost in costs),
            "estimated_usd": {actor: sum(c[actor] for c in costs) for actor in ("bank", "customer", "total")
                              if all(actor in cost for cost in costs)},
            "batches": provenance,
        }
    return {"score_version": SCORE_VERSION, "cohorts": cohorts}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("audits/evaluations"))
    args = parser.parse_args()
    print(json.dumps(summarize(args.root), indent=2))
