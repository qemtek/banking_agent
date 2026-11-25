"""Freeze matched refund worlds or score both sides of each pair."""
import argparse
import json
from collections import defaultdict
from pathlib import Path

from evaluation.casebooks import digest
from evaluation.usage import summarize
from .instances import VERSION, world


def freeze(seed, pairs):
    cases = []
    messages = ["Please check the draft and my statement. Has my disputed purchase been refunded in full? "
                "Leave the draft as it is for now.",
                "Close the dispute only if that purchase was fully refunded. Otherwise leave it open. "
                "Please tell me the saved status."]
    for world_seed in range(seed, seed + pairs):
        for variant in ("refund_linked", "refund_unrelated"):
            facts = world(world_seed, "medium", variant)
            statement = facts["statement"]
            if variant == "refund_linked":
                statement["rows"] = [(*r[:-1], statement["target"]) if r[0] == statement["refund"] else r
                                     for r in statement["rows"]]
            case = {"family": "refund", "variant": variant, "difficulty": "medium", "behaviour": "cooperative",
                    "split": "development", "world_seed": world_seed, "world": facts, "messages": messages,
                    "conditional_withdrawal": True}
            cases.append({**case, "case_id": digest([VERSION, case])})
    return {"version": VERSION, "split": "development", "seed": seed, "cases": cases}


def results(report):
    rows = json.loads(report.read_text())
    manifest = json.loads(report.with_name("manifest.json").read_text())
    book = json.loads(report.with_name("casebook.json").read_text())
    if digest(book) != manifest["casebook_id"] or not all(c.get("conditional_withdrawal") for c in book["cases"]):
        raise ValueError("Expected a frozen conditional-withdrawal casebook")
    expected = {(c["case_id"], i) for c in book["cases"] for i in range(1, manifest["repeats"] + 1)}
    if len(rows) != len(expected) or {(r["case_id"], r["repeat"]) for r in rows} != expected:
        raise ValueError("Report is incomplete or contains duplicate attempts")
    pairs = defaultdict(dict)
    for r in rows:
        outcome = r["dimensions"]["operational"]
        if outcome == "PASS" and any(c["category"] == "delivery" and c["passed"] is False for c in r["checks"]):
            outcome = "FAIL"
        pairs[r["world_seed"], r["repeat"]][r["variant"]] = outcome
    if any(set(sides) != {"refund_linked", "refund_unrelated"} for sides in pairs.values()):
        raise ValueError("Both sides are required")
    passed = {k: all(v == "PASS" for v in sides.values()) for k, sides in pairs.items()}
    first = [v for (_, repeat), v in passed.items() if repeat == 1]
    seeds = {seed for seed, _ in pairs}
    return {"pairs": len(seeds), "repeats": manifest["repeats"], "both_sides_first_attempt": sum(first),
            "both_sides_every_repeat": sum(all(passed[s, i] for i in range(1, manifest["repeats"] + 1))
                                           for s in seeds),
            "execution_errors": sum(r["status"] == "ERROR" for r in rows),
            "estimated_cost_usd": summarize(rows, manifest)["estimated_cost_usd"],
            "outcomes": [{"seed": s, "repeat": i, **values} for (s, i), values in sorted(pairs.items())]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("freeze")
    gen.add_argument("file", type=Path)
    gen.add_argument("--seed", type=int, default=26112510)
    gen.add_argument("--pairs", type=int, default=3)
    commands.add_parser("score").add_argument("report", type=Path)
    args = parser.parse_args()
    if args.command == "score":
        print(json.dumps(results(args.report), indent=2))
    else:
        if args.pairs < 1:
            parser.error("pairs must be positive")
        args.file.parent.mkdir(parents=True, exist_ok=True)
        with args.file.open("x") as output:
            json.dump(freeze(args.seed, args.pairs), output, indent=2)
            output.write("\n")
