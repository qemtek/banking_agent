"""Blind review of delivered answers and customer fidelity on saved runs."""
import argparse
import json
import random
from collections import Counter
from pathlib import Path

from .casebooks import digest, trial_directory, validate_contents
from .scoring import Check, SCORE_VERSION, scores

LABELS = (None, "pass", "fail", "uncertain")
CUSTOMER = (None, "followed_brief", "deviated", "uncertain")


def review_items(report, rows):
    cases = {}
    manifest = json.loads((report.parent / "manifest.json").read_text())
    if manifest.get("suite") == "generated":
        book = json.loads((report.parent / "casebook.json").read_text())
        validate_contents(book)
        if digest(book) != manifest["casebook_id"]:
            raise ValueError("Casebook differs from manifest")
        cases = {c["case_id"]: c for c in book["cases"]}
    items = {}
    for index, row in enumerate(rows):
        if row.get("score_version", manifest.get("score_version")) not in (5, 6, 7, 8, 9, SCORE_VERSION) or "dimensions" not in row:
            raise ValueError("Use a current scorecard or regrade the saved batch first")
        if row["status"] == "ERROR":
            continue
        key = digest([row["turn_audits"], row["dialogue"]])
        if key in items:
            raise ValueError("Duplicate conversation")
        criteria = []
        for i, check in enumerate(row["checks"]):
            if check["category"] == "communication":
                if check["passed"] is not None:
                    raise ValueError("Review requires an unlabelled source report")
                criteria.append({"index": i, "criterion": check["criterion"], "expected": check["expected"],
                                 "delivered": check["evidence"], "label": None})
        case = cases.get(row.get("case_id"), {})
        item = {"id": key, "dialogue": row["dialogue"], "criteria": criteria}
        if row.get("customer_mode") == "llm":
            if case:
                item["customer_brief"] = {"behaviour": case["behaviour"], "objectives": case["messages"]}
            else:
                item["customer_brief"] = manifest.get("customer", {})
            # Show the simulator's actual instructions to distinguish intended deception from drift.
            trial = trial_directory(report.parent, row)
            instructions = []
            for audit in row.get("customer_audits", []):
                events = (trial / "customers" / Path(audit).name).read_text().splitlines()
                for event in map(json.loads, events):
                    if event["event_type"] == "model.requested":
                        instructions.append(event["payload"]["messages"][0]["content"])
                    elif event["event_type"] == "message.fixed":
                        instructions.append(event["payload"]["text"])
            item["customer_brief"]["turn_instructions"] = instructions
            item["customer_fidelity"] = None
        items[key] = index, item
    return items


def selection(rows, size, seed):
    worlds = sorted({world_key(r) for r in rows})
    if size is not None and not 1 <= size <= len(worlds):
        raise ValueError("Sample size must fit the number of worlds")
    random.Random(seed).shuffle(worlds)
    return set(worlds[:size])


def world_key(row):
    return row.get("case_id") or digest([row["task"], row.get("customer_mode", "scripted")])


def packet(report, size=None, seed=41):
    rows = json.loads(report.read_text())
    chosen = selection(rows, size, seed)
    items = [item for i, item in review_items(report, rows).values() if world_key(rows[i]) in chosen]
    random.Random(seed).shuffle(items)
    return {"source_digest": digest(rows), "size": size, "seed": seed, "items": items}


def apply(report, labels, reviewer):
    rows = json.loads(report.read_text())
    if not reviewer.strip() or labels["source_digest"] != digest(rows):
        raise ValueError("Supply a reviewer and labels for this exact source report")
    chosen = selection(rows, labels["size"], labels["seed"])
    items = review_items(report, rows)
    expected = {key for key, (i, _) in items.items() if world_key(rows[i]) in chosen}
    if {item["id"] for item in labels["items"]} != expected:
        raise ValueError("The packet must include the entire selected sample. Leave open labels blank.")
    design = digest([sorted(chosen), labels["size"], labels["seed"]])
    for row in rows:
        row["review_sample"] = world_key(row) in chosen
        row["review_design"] = design
    seen = set()
    counts = Counter()
    for item in labels["items"]:
        key = item["id"]
        if key not in items or key in seen:
            raise ValueError("Duplicate or unknown conversation label")
        seen.add(key)
        i, original = items[key]
        row = rows[i]
        normalized = {**item, "criteria": [{**c, "label": None} for c in item["criteria"]]}
        if "customer_fidelity" in original:
            if item.get("customer_fidelity") not in CUSTOMER:
                raise ValueError("Unknown customer fidelity label")
            normalized["customer_fidelity"] = None
        if normalized != original:
            raise ValueError("Reviewer changed evidence or rubric")
        for criterion in item["criteria"]:
            label = criterion["label"]
            if label not in LABELS:
                raise ValueError("Labels must be pass, fail, uncertain or null")
            c = row["checks"][criterion["index"]]
            counts["unassessed" if label is None else label] += 1
            if label in ("pass", "fail"):
                c["passed"] = label == "pass"
                hint = c["wording_signal"]
                if hint is not None:
                    counts[("true_" if hint == c["passed"] else "false_") + ("pass" if hint else "fail")] += 1
            c["review_label"] = label
        if "customer_fidelity" in item:
            row["customer_fidelity"] = item["customer_fidelity"]
        checks = [Check(**{k: c[k] for k in Check.__dataclass_fields__ if k in c}) for c in row["checks"]]
        row["status"], row["dimensions"] = scores(checks)
        row["reviewer"] = reviewer
        row["review_source_digest"] = labels["source_digest"]
    return rows, {"source_digest": labels["source_digest"], "reviewer": reviewer,
                  "selected_conversations": len(seen), "eligible_conversations": len(items),
                  "criterion_counts": dict(counts),
                  "customer_fidelity": dict(Counter(r.get("customer_fidelity") or "unassessed" for r in rows
                                                     if r.get("customer_mode") == "llm" and r["status"] != "ERROR"))}


def write_new(path, value):
    with path.open("x") as f:
        json.dump(value, f, indent=2)
        f.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("report", type=Path)
    export.add_argument("packet", type=Path)
    export.add_argument("--sample", type=int)
    export.add_argument("--seed", type=int, default=41)
    grade = sub.add_parser("apply")
    grade.add_argument("report", type=Path)
    grade.add_argument("packet", type=Path)
    grade.add_argument("--reviewer", required=True)
    grade.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "export":
        write_new(args.packet, packet(args.report, args.sample, args.seed))
    else:
        if args.output.resolve().parent != args.report.resolve().parent:
            parser.error("Keep the reviewed report alongside the source manifest and evidence")
        rows, summary = apply(args.report, json.loads(args.packet.read_text()), args.reviewer)
        write_new(args.output, rows)
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
