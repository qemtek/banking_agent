"""Compare direct and compound versions of the same missing-card development worlds."""
import argparse
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median

from evaluation.casebooks import trial_directory
from evaluation.tracking import turn_limit_metrics
from evaluation.uplift import load_arm
from evaluation.usage import report_cost
from .missing_card_pilot import DESIGN
from .missing_card_worlds import DISPOSITIONS, MISSING_CARD_VARIANTS


def summarize_cases(observations, batch, manifest):
    verdicts, assessed, failed = Counter(), Counter(), Counter()
    requests, costs = [], []
    for row in observations:
        status = _operational(row)
        verdicts[status] += 1
        if status in ("PASS", "FAIL"):
            criteria = defaultdict(list)
            for check in row["checks"]:
                if check["passed"] is not None:
                    criteria[check["category"]].append(check["passed"])
            for name, values in criteria.items():
                assessed[name] += 1
                failed[name] += not all(values)
        trial = trial_directory(batch, row)
        metrics = turn_limit_metrics([trial / Path(audit).name for audit in row["turn_audits"]])
        if "bank_tool_requests" in metrics:
            requests.append(metrics["bank_tool_requests"])
        cost = report_cost(row, manifest).get("total")
        if cost is not None:
            costs.append(cost)
    return {"conversations": len(observations), "operational": dict(verdicts),
            "category_counts": {key: {"assessed": count, "failed": failed[key]} for key, count in assessed.items()},
            "requests_measured": len(requests), "tool_requests": sum(requests),
            "median_tool_requests": median(requests) if requests else None,
            "priced": len(costs), "known_cost_usd": math.fsum(costs)}


BOOTSTRAP_DRAWS = 10000


def _operational(row):
    if row["status"] == "ERROR":
        return "ERROR"
    if row.get("customer_fidelity") == "deviated":
        return "SIMULATOR_DEVIATION"
    return row["dimensions"]["operational"]


def _paired_counts(pairs):
    known = {"PASS": 1, "FAIL": 0}
    transitions = Counter()
    missing = {"direct": Counter(), "compound": Counter()}
    differences, bounds = [], []
    for direct, compound in pairs:
        a, b = _operational(direct), _operational(compound)
        if a not in known:
            missing["direct"][a] += 1
        if b not in known:
            missing["compound"][b] += 1
        # Each absent side takes its worst and best possible binary score independently.
        bounds.append((known.get(b, 0) - known.get(a, 1), known.get(b, 1) - known.get(a, 0)))
        if a in known and b in known:
            transitions[f"{a}/{b}"] += 1
            differences.append(known[b] - known[a])
    n = len(pairs)
    low, high = (mean(end) for end in zip(*bounds))
    radius = math.sqrt(2 * math.log(40) / n)
    stats = {
        "base_worlds": n, "complete_pairs": len(differences), "missing_pairs": n - len(differences),
        "direct_compound_counts": {f"{a}/{b}": transitions[f"{a}/{b}"] for a in known for b in known},
        "assessed_siblings": {side: n - sum(reasons.values()) for side, reasons in missing.items()},
        "missing_siblings": {side: dict(reasons) for side, reasons in missing.items()},
        "complete_case_mean_pp": 100 * mean(differences) if differences else None,
        "compound_minus_direct_pp": 100 * low if len(differences) == n else None,
        "missing_score_bounds_pp": [100 * low, 100 * high],
        "paired_hoeffding_95_pp": [100 * max(-1, low - radius), 100 * min(1, high + radius)],
    }
    return stats, differences


def _bootstrap(strata, seed):
    # Each value is a whole base world's compound-minus-direct difference. Preserve
    # the frozen equal weights of dispositions even if their complete-case counts vary.
    rng = random.Random(seed)
    samples = []
    for _ in range(BOOTSTRAP_DRAWS):
        means = [sum(rng.choices(values, k=len(values))) / len(values) for values in strata]
        samples.append(100 * sum(means) / len(means))
    samples.sort()
    return [samples[249], samples[9749]]


def matched_results(pairs, seed):
    by_disposition = defaultdict(list)
    for direct, compound in pairs:
        by_disposition[direct["variant"]].append((direct, compound))
    stats, _ = _paired_counts(pairs)
    breakdown, strata = {}, []
    for disposition, sample in sorted(by_disposition.items()):
        breakdown[disposition], differences = _paired_counts(sample)
        strata.append(sorted(differences))
    stats["by_disposition"] = breakdown
    stats["empty_complete_case_strata"] = [key for key, counts in breakdown.items() if not counts["complete_pairs"]]
    stats["complete_case_equal_disposition_mean_pp"] = 100 * mean(mean(s) for s in strata) if all(strata) else None
    stats["paired_stratified_bootstrap_95_pp"] = _bootstrap(strata, seed) if all(strata) else None
    stats["bootstrap"] = {"sample": "complete pairs within each issuer disposition", "draws": BOOTSTRAP_DRAWS,
                          "seed": seed, "interval": "equal-disposition-weighted 2.5% and 97.5% nearest ranks"}
    return stats


def report(path):
    path = Path(path)
    manifest, observations = load_arm(path)
    batch = path if path.is_dir() else path.parent
    book = json.loads((batch / "casebook.json").read_text())
    if book.get("design") != DESIGN or book["split"] != "development" or manifest.get("repeats") != 1:
        raise ValueError("The missing-card pilot uses one attempt per sibling on development worlds")
    groups = defaultdict(lambda: defaultdict(dict))
    levels = defaultdict(list)
    for case in book["cases"]:
        family = case["family"]
        if family not in MISSING_CARD_VARIANTS:
            raise ValueError("Expected missing-card pilot tasks")
        siblings = groups[family][case["world_seed"]]
        side = case["sibling"]
        if side not in ("direct", "compound") or side in siblings:
            raise ValueError("Invalid or repeated pilot sibling")
        row = observations[case["case_id"], 1]
        siblings[side] = row
        levels[case["difficulty"]].append(row)
    if groups.keys() != MISSING_CARD_VARIANTS.keys():
        raise ValueError("The paired pilot requires both missing-card families")
    families = {}
    for family, worlds in groups.items():
        pairs = []
        for siblings in worlds.values():
            if siblings.keys() != {"direct", "compound"}:
                raise ValueError("Incomplete direct/compound pair")
            direct, compound = siblings["direct"], siblings["compound"]
            if direct["variant"] != compound["variant"] or direct["behaviour"] != compound["behaviour"]:
                raise ValueError("Paired cases must share the disposition and customer style")
            if direct["difficulty"] != "easy" or compound["difficulty"] not in ("medium", "hard"):
                raise ValueError("Each compound case requires an easy direct control")
            pairs.append((direct, compound))
        counts = Counter((row["variant"], row["difficulty"]) for _, row in pairs)
        if counts != {(v, d): 2 for v in DISPOSITIONS for d in ("medium", "hard")}:
            raise ValueError("Expected two medium and two hard pairs per issuer disposition")
        families[family] = {"matched": matched_results(pairs, book["seed"])}
        for side in ("direct", "compound"):
            families[family][side] = summarize_cases([v[side] for v in worlds.values()], batch, manifest)
    return {"casebook_id": manifest["casebook_id"], "source_sha256": manifest["source_sha256"],
            "families": families, "difficulty": {k: summarize_cases(v, batch, manifest) for k, v in levels.items()},
            "overall": summarize_cases(list(observations.values()), batch, manifest)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    print(json.dumps(report(args.report), indent=2))
