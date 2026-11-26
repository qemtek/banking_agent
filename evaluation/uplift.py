"""Compare binary outcomes on identical frozen cases and matched repeat numbers."""
import argparse
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, stdev

from .casebooks import digest, validate_contents
from .tracking import evaluation_metrics

METRICS = ("task_pass", "operational_correct", "communication_correct", "initial_amount_correct", "initial_evidence_checked", "committed_writes_correct",
           "routing_decision_correct", "routing_queue_correct", "corrected_resolution")


def load_arm(path):
    path = Path(path)
    directory = path if path.is_dir() else path.parent
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("suite") != "generated":
        raise ValueError("Uplift needs a generated report with frozen case IDs")
    book = json.loads((directory / "casebook.json").read_text())
    validate_contents(book)
    if (digest(book) != manifest["casebook_id"] or manifest["split"] != book["split"]
            or manifest["generator_version"] != book["version"]):
        raise ValueError("Frozen casebook differs from the run manifest")
    cases = {c["case_id"]: c for c in book["cases"]}
    rows = {}
    report = directory / "report.json" if path.is_dir() else path
    items = json.loads(report.read_text())
    versions = {r.get("score_version", manifest["score_version"]) for r in items}
    if len(versions) != 1:
        raise ValueError("Inconsistent report score versions")
    manifest["score_version"] = versions.pop()
    replay_sources = {r.get("replay_source_sha256") for r in items}
    if len(replay_sources) != 1:
        raise ValueError("Inconsistent replay source across observations")
    manifest["replay_source_sha256"] = replay_sources.pop()
    for row in items:
        key = (row["case_id"], row["repeat"])
        if key in rows or key[0] not in cases or key[1] < 1:
            raise ValueError("Duplicate or invalid observation key")
        if row["status"] not in ("PASS", "FAIL", "ERROR", "REVIEW"):
            raise ValueError("Unknown execution status")
        case = cases[key[0]]
        if any(case[k] != row[k] for k in ("world_seed", "family", "variant", "behaviour", "difficulty")):
            raise ValueError("Result metadata differs from the frozen instance")
        rows[key] = row
    repeats = manifest.get("repeats", max((r for _,r in rows), default=0))
    if not rows or rows.keys() != {(k, n) for k in cases for n in range(1, repeats + 1)}:
        raise ValueError("Incomplete casebook or missing repeats")
    return manifest, rows


def binary_score(row, metric):
    if row["status"] == "ERROR":
        return None
    if metric == "task_pass":
        return None if row["status"] == "REVIEW" else int(row["status"] == "PASS")
    return evaluation_metrics(row).get(metric)


def exact_p(wins, losses):
    n = wins + losses
    if n == 0:
        return 1.0
    numerator = sum(math.comb(n, i) for i in range(min(wins, losses) + 1))
    return min(1.0, (2 * numerator) / (1 << n))


def bootstrap_interval(strata):
    # Preserve the case mix and resample whole cases, including all their repeats.
    if any(len(values) < 2 for values in strata.values()) or all(len(set(v)) == 1 for v in strata.values()):
        return None
    rng = random.Random(41)
    size = sum(map(len, strata.values()))
    samples = sorted(100 * sum(sum(rng.choices(values, k=len(values))) for values in strata.values()) / size
                     for _ in range(2000))
    return [samples[49], samples[1949]]


def compare(baseline, candidate, metric="task_pass", *, filters=None, review_sample=False):
    if metric not in METRICS:
        raise ValueError(f"Unknown metric {metric}")
    a, left = load_arm(baseline)
    b, right = load_arm(candidate)
    fixed = ("casebook_id", "generator_version", "score_version", "model", "reasoning_effort",
             "customer_mode", "actor_version")
    if a.get("card_selection_version", 1) != b.get("card_selection_version", 1):
        raise ValueError("Pairing requires the same card-selection interface and rubric")
    if any(a[k] != b[k] for k in fixed) or left.keys() != right.keys():
        raise ValueError("Pairing requires identical cases, repeats, scorer, customer and model settings")
    for setting in ("backend", "sandbox_image", "env0_image", "benchflow_version",
                    "max_tool_calls", "window", "reserve", "unit", "model_timeout_seconds", "session_date_source",
                    "replay_source_sha256"):
        if a.get(setting) != b.get(setting):
            raise ValueError(f"Pairing requires matching {setting}")
    if Path(baseline).resolve() == Path(candidate).resolve() or a["arm"] == b["arm"]:
        raise ValueError("Supply distinct named baseline and candidate executions")
    if review_sample:
        designs = {r.get("review_design") for r in [*left.values(), *right.values()]}
        if None in designs or len(designs) != 1 or any(left[k]["review_sample"] != right[k]["review_sample"] for k in left):
            raise ValueError("Both arms need the same preselected review sample")
        left = {k: r for k, r in left.items() if r["review_sample"]}
    filters = filters or {}
    left = {key: row for key, row in left.items() if all(row[k] == value for k, value in filters.items())}
    if not left:
        raise ValueError("No cases match the selected dimensions")
    errors = Counter()
    cases = defaultdict(list)
    for key, row in left.items():
        other = right[key]
        if row["status"] == "ERROR":
            errors["baseline"] += 1
        if other["status"] == "ERROR":
            errors["candidate"] += 1
        cases[key[0]].append((binary_score(row, metric), binary_score(other, metric)))
    coverage = {arm: dict(Counter(row["status"] for row in observations))
                for arm, observations in (("baseline", left.values()), ("candidate", [right[k] for k in left]))}
    fidelity = {arm: dict(Counter(row.get("customer_fidelity") or "unassessed" for row in observations
                                if row.get("customer_mode") == "llm" and row["status"] != "ERROR"))
               for arm, observations in (("baseline", left.values()), ("candidate", [right[k] for k in left]))}
    rates = []
    differences_by_cell = defaultdict(list)
    transitions = Counter()
    for case_id, pairs in sorted(cases.items()):
        # Equal weight per world; retain only complete clusters for the selected endpoint.
        if any(l is None or r is None for l,r in pairs):
            continue
        baseline_mean, candidate_mean = mean(l for l,r in pairs), mean(r for l,r in pairs)
        rates.append((baseline_mean, candidate_mean))
        row = left[(case_id, 1)]
        cell = (row["variant"], row["behaviour"], row["difficulty"])
        differences_by_cell[cell].append(candidate_mean - baseline_mean)
        for l,r in pairs:
            transitions["win" if r > l else "loss" if r < l else "tie"] += 1
    n = len(rates)
    result = {"metric": metric, "baseline": a["arm"], "candidate": b["arm"], "casebook_id": a["casebook_id"],
              "split": a["split"], "customer_mode": a["customer_mode"], "score_version": a["score_version"],
              "trial_statuses": coverage, "customer_fidelity": fidelity, "filters": filters, "total_cases": len(cases), "matched_observations": len(left),
              "complete_cases": n, "excluded_cases": len(cases) - n,
              "execution_errors": {arm: errors[arm] for arm in ("baseline", "candidate")},
              "transitions": {k: transitions[k] for k in ("win", "loss", "tie")}}
    bounds = [[], []]
    for pairs in cases.values():
        bounds[0].append(mean((0 if r is None else r) - (1 if l is None else l) for l, r in pairs))
        bounds[1].append(mean((1 if r is None else r) - (0 if l is None else l) for l, r in pairs))
    result["possible_uplift_pp"] = [100 * mean(side) for side in bounds]
    result["review_sample"] = review_sample
    pending = any(r.get("dimensions", {}).get("communication") == "REVIEW"
                  for k in left for r in (left[k], right[k])
                  if metric == "communication_correct" or metric == "task_pass" and r["status"] == "REVIEW")
    if pending:
        result["inference_withheld"] = "Required communication reviews are pending in the selected population"
    if not rates or pending:
        return {**result, "uplift_pp": None, "ci95_pp": None, "paired_bootstrap_ci95_pp": None, "p_exact": None}
    base, cand = mean(l for l,r in rates), mean(r for l,r in rates)
    delta = cand - base
    # Independent case means each lie in [-1,1]. Hoeffding also covers a fixed, stratified design.
    margin = math.sqrt(2 * math.log(40) / n)
    single = len(left) == len(cases)
    differences = [r - l for l, r in rates]
    return {**result, "baseline_rate": base, "candidate_rate": cand, "uplift_pp": 100 * delta,
            "paired_standard_error_pp": 100 * stdev(differences) / math.sqrt(n) if n > 1 else None,
            "paired_bootstrap_ci95_pp": bootstrap_interval(differences_by_cell),
            "ci95_pp": [100 * max(-1, delta-margin), 100 * min(1, delta+margin)],
            "p_exact": exact_p(transitions["win"], transitions["loss"]) if single else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--metric", choices=METRICS, default="task_pass")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--review-sample", action="store_true", help="Compare the same preselected review worlds in both arms")
    dimensions = ("family", "variant", "behaviour", "difficulty")
    for dimension in dimensions:
        parser.add_argument("--" + dimension)
    args = parser.parse_args()
    try:
        filters = {k: getattr(args, k) for k in dimensions if getattr(args, k)}
        result = compare(args.baseline, args.candidate, args.metric, filters=filters, review_sample=args.review_sample)
    except ValueError as error:
        parser.error(str(error))
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
