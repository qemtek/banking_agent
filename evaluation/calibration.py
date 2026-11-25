"""Summarize independent baseline cases, preselected repeats and operational retries."""
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from .uplift import load_arm
from .casebooks import trial_directory


def calibration(primary, repeats, primary_retries=(), repeat_retries=(), distractors=()):
    settings = None
    used_reports = set()

    def load(paths, retries, ids=None):
        nonlocal settings
        observations = {}
        attempts = []
        for retry, directories in ((False, paths), (True, retries)):
            for path in directories:
                path = Path(path)
                report = path
                if path.is_file():
                    path = path.parent
                if path.resolve() in used_reports:
                    raise ValueError("Supply distinct executions for calibration and repeats")
                used_reports.add(path.resolve())
                manifest, rows = load_arm(report)
                fixed = {k: manifest[k] for k in ("generator_version", "score_version", "model", "reasoning_effort",
                                                   "bank_prompt_version", "instruction_hash", "actor_version",
                                                   "customer_mode", "split")}
                if fixed["split"] != "development":
                    raise ValueError("Calibration needs development cases")
                if settings is not None and fixed != settings:
                    raise ValueError("Inconsistent baseline or environment versions")
                settings = fixed
                for (case_id, repeat), row in rows.items():
                    if repeat != 1 or (ids is not None and case_id not in ids):
                        raise ValueError("Expected one observation of a selected frozen world")
                    previous = observations.get(case_id)
                    if (retry and (previous is None or previous[0]["status"] != "ERROR")
                            or not retry and previous is not None):
                        raise ValueError("Duplicate observation or retry of a scored case")
                    observations[case_id] = row, path
                    attempts.append({"case_id": case_id, "variant": row["variant"], "difficulty": row["difficulty"],
                                     "retry": retry, "status": row["status"], "report": str(report if report.is_file() else path / "report.json")})
        return observations, attempts

    first, first_attempts = load(primary, primary_retries)
    again, repeat_attempts = load(repeats, repeat_retries, first)
    by_level = defaultdict(Counter)
    failures = []
    requested = Counter()
    for case_id, (row, path) in first.items():
        by_level[row["difficulty"]][row["status"]] += 1
        if row["status"] == "FAIL":
            failures.append({"case_id": case_id, "variant": row["variant"], "difficulty": row["difficulty"],
                             "failed": [c["criterion"] for c in row["checks"] if c["passed"] is False and c.get("category") not in ("efficiency", "unnecessary_work")]})
        for audit in row["turn_audits"]:
            for line in (trial_directory(path, row) / Path(audit).name).read_text().splitlines():
                event = json.loads(line)
                if event["event_type"] == "tool.requested":
                    requested[event["payload"]["name"]] += 1
    transitions = Counter()
    for case_id, (row, _) in again.items():
        transitions[first[case_id][0]["status"] + " to " + row["status"]] += 1
    return {"settings": settings, "worlds": len(first), "selected_repeats": len(again),
            "primary_by_difficulty": by_level, "repeat_outcomes": transitions, "primary_failures": failures,
            "primary_tool_requests": requested, "primary_distractor_requests": {k: requested[k] for k in distractors},
            "attempts": {"primary": first_attempts, "repeat": repeat_attempts}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary", nargs="+", required=True, type=Path)
    parser.add_argument("--repeats", nargs="*", default=[], type=Path)
    parser.add_argument("--primary-retries", nargs="*", default=[], type=Path)
    parser.add_argument("--repeat-retries", nargs="*", default=[], type=Path)
    parser.add_argument("--distractors", nargs="*", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = calibration(args.primary, args.repeats, args.primary_retries, args.repeat_retries, args.distractors)
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
