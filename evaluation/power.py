"""Plan a one-attempt paired holdout from a matched development pilot."""
import argparse
import json
import math
import random
from collections import Counter
from itertools import count, islice
from pathlib import Path
from statistics import NormalDist, mean, quantiles

from .uplift import METRICS, binary_score, compare, load_arm
from .usage import report_cost


DIMENSIONS = ("family", "variant", "behaviour", "difficulty")
EXACT_SEARCH_LIMIT = 2_500
EXACT_METHOD = "exact_mcnemar_iid"
APPROX_METHOD = "normal_approximation"
LARGE_SAMPLE_METHOD = "normal_approximation_large_sample"


def matches(row, filters):
    return all(row[key] == value for key, value in filters.items())


def groups_for(rows, mixture):
    if mixture is None:
        return [("selected cases", 1.0, {}, list(rows))]
    if not isinstance(mixture, list) or not mixture:
        raise ValueError("A mixture must be a nonempty list of weighted groups")
    groups = []
    used = set()
    for group in mixture:
        if not isinstance(group, dict) or set(group) != {"name", "weight", "filters"}:
            raise ValueError("Each mixture group needs name, weight and filters")
        name, weight, filters = (group[key] for key in ("name", "weight", "filters"))
        if not isinstance(name, str) or not name or any(name == item[0] for item in groups):
            raise ValueError("Mixture groups need distinct nonempty names")
        if type(weight) not in (int, float) or not 0 < weight <= 1:
            raise ValueError("Mixture weights must be positive numbers")
        if (not isinstance(filters, dict) or set(filters) - set(DIMENSIONS)
                or any(not isinstance(value, str) or not value for value in filters.values())):
            raise ValueError(f"Invalid mixture filters for {name}")
        keys = [key for key, row in rows.items() if matches(row, filters)]
        if not keys:
            raise ValueError(f"No pilot cases matched mixture group {name}")
        if used.intersection(keys):
            raise ValueError(f"Pilot cases overlap between mixture groups at {name}")
        used.update(keys)
        groups.append((name, weight, filters, keys))
    total = sum(weight for _, weight, _, _ in groups)
    if not math.isclose(total, 1.0, rel_tol=0, abs_tol=1e-9):
        raise ValueError("Mixture weights must sum to 1")
    return [(name, weight / total, filters, keys) for name, weight, filters, keys in groups]


def wilson(count, total, z):
    p = count / total
    z2 = z * z
    centre = p + z2 / (2 * total)
    radius = z * math.sqrt((p * (1 - p) + z2 / (4 * total)) / total)
    scale = 1 + z2 / total
    return [max(0, (centre - radius) / scale), min(1, (centre + radius) / scale)]


def paired_sample_size(delta, discordance, alpha, power):
    """Normal approximation with a one-count continuity correction."""
    normal = NormalDist()
    critical = -normal.inv_cdf(alpha / 2)
    desired = normal.inv_cdf(power)
    variation = critical * math.sqrt(discordance) + desired * math.sqrt(discordance - delta * delta)
    root = (variation + math.sqrt(variation * variation + 4 * delta)) / (2 * delta)
    try:
        return math.ceil(root * root)
    except OverflowError as error:
        raise ValueError("The target is too small to represent the required sample size") from error


def binomial_step(probabilities, success):
    failure = 1 - success
    neighbours = zip(probabilities, islice(probabilities, 1, None))
    return [probabilities[0] * failure,
            *(left * success + right * failure for left, right in neighbours),
            probabilities[-1] * success]


def exact_mcnemar_powers(delta, discordance, alpha):
    """Yield (worlds, power), averaging exact rejection over the discordant count."""
    win_given_discordance = (discordance + delta) / (2 * discordance)
    alpha_numerator, alpha_denominator = alpha.as_integer_ratio()
    discordant_counts = [1.0]
    conditional_wins = [1.0]
    rejection = [0.0]
    cutoff = -1
    null_tail_count = 0
    for worlds in count(1):
        discordant_counts = binomial_step(discordant_counts, discordance)
        conditional_wins = binomial_step(conditional_wins, win_given_discordance)
        # The largest lower-tail cutoff can rise by at most one after a fair coin flip.
        if cutoff >= 0:
            null_tail_count = 2 * null_tail_count - math.comb(worlds - 1, cutoff)
        next_tail_count = null_tail_count + math.comb(worlds, cutoff + 1)
        if 2 * next_tail_count * alpha_denominator <= alpha_numerator << worlds:
            cutoff += 1
            null_tail_count = next_tail_count
        if cutoff < 0:
            rejection.append(0.0)
        else:
            rejection.append(math.fsum(conditional_wins[:cutoff + 1])
                             + math.fsum(conditional_wins[worlds - cutoff:]))
        yield worlds, math.fsum(chance * reject for chance, reject in zip(discordant_counts, rejection))


def exact_paired_sample_size(delta, discordance, alpha, power):
    # Discrete rejection boundaries can make power decline when a world is added.
    return next(worlds for worlds, achieved in exact_mcnemar_powers(delta, discordance, alpha) if achieved >= power)


def planning_size(delta, discordance, alpha, power, iid):
    approximation = paired_sample_size(delta, discordance, alpha, power)
    if not iid:
        return approximation, APPROX_METHOD
    # The exact search is quadratic. Check each smaller sample to respect discrete cutoffs.
    if approximation <= EXACT_SEARCH_LIMIT:
        candidates = islice(exact_mcnemar_powers(delta, discordance, alpha), EXACT_SEARCH_LIMIT)
        exact = next((worlds for worlds, achieved in candidates if achieved >= power), None)
        if exact is not None:
            return exact, EXACT_METHOD
    return approximation, LARGE_SAMPLE_METHOD


def summarize_group(name, weight, filters, keys, left, right, manifests, metric):
    scores = [(binary_score(left[key], metric), binary_score(right[key], metric)) for key in keys]
    complete = [(base, cand) for base, cand in scores if base is not None and cand is not None]
    transitions = {
        "win": complete.count((0, 1)), "loss": complete.count((1, 0)),
        "both_pass": complete.count((1, 1)), "both_fail": complete.count((0, 0)),
    }
    statuses = {arm: dict(Counter(rows[key]["status"] for key in keys))
                for arm, rows in (("baseline", left), ("candidate", right))}
    missing = {"baseline": sum(base is None for base, _ in scores),
               "candidate": sum(cand is None for _, cand in scores)}
    bounds = [mean((0 if cand is None else cand) - (1 if base is None else base) for base, cand in scores),
              mean((1 if cand is None else cand) - (0 if base is None else base) for base, cand in scores)]
    costs = {}
    prices = []
    for arm, rows, manifest in zip(("baseline", "candidate"), (left, right), manifests):
        values = [report_cost(rows[key], manifest).get("total") for key in keys]
        prices.append(values)
        known = [value for value in values if value is not None]
        costs[arm] = {"priced_attempts": len(known), "known_total_usd": sum(known),
                      "usd_per_attempt": mean(known) if len(known) == len(keys) else None}
    paired_prices = [base + cand for base, cand in zip(*prices) if base is not None and cand is not None]
    return {"name": name, "weight": weight, "filters": filters, "worlds": len(keys),
            "complete_pairs": len(complete), "missing_scores": missing, "trial_statuses": statuses,
            "transitions": transitions, "possible_uplift": bounds, "model_cost": costs,
            "fully_priced_pairs": len(paired_prices)}, sorted(paired_prices)


def paired_cost_summary(prices, weights, holdout_sizes):
    draws = 2_000
    seed = 41
    summary = {"fully_priced_pairs": sum(map(len, prices)),
               "known_total": math.fsum(value for group in prices for value in group),
               "mean_per_pair": None, "mean_per_pair_ci95": None,
               "bootstrap_draws": draws, "bootstrap_seed": seed,
               "method": ("Percentile bootstrap of fully priced baseline–candidate pairs within each design group. "
                          "The holdout interval covers expected model cost at the planned sample size and pilot prices. "
                          "Pricing changes, sizing uncertainty and future-run variation are outside this interval.")}
    if all(prices):
        summary["mean_per_pair"] = math.fsum(weight * mean(values) for weight, values in zip(weights, prices))
    if any(len(values) < 2 for values in prices):
        return summary, None

    rng = random.Random(seed)
    mean_costs = []
    holdout_costs = []
    for _ in range(draws):
        group_means = [math.fsum(rng.choices(values, k=len(values))) / len(values) for values in prices]
        mean_costs.append(math.fsum(weight * cost for weight, cost in zip(weights, group_means)))
        if holdout_sizes is not None:
            holdout_costs.append(math.fsum(size * cost for size, cost in zip(holdout_sizes, group_means)))
    cutoffs = quantiles(mean_costs, n=40, method="inclusive")
    summary["mean_per_pair_ci95"] = [cutoffs[0], cutoffs[-1]]
    if not holdout_costs:
        return summary, None
    cutoffs = quantiles(holdout_costs, n=40, method="inclusive")
    return summary, [cutoffs[0], cutoffs[-1]]


def holdout_allocation(groups, pairs):
    # Largest remainders preserve the overall sample size and keep each quota within one world.
    quotas = [pairs * group["weight"] for group in groups]
    counts = [math.floor(quota) for quota in quotas]
    remaining = pairs - sum(counts)
    order = sorted(range(len(quotas)), key=lambda index: quotas[index] - counts[index], reverse=True)
    for index in order[:remaining]:
        counts[index] += 1
    return {group["name"]: count for group, count in zip(groups, counts)}


def plan(baseline, candidate, metric="operational_correct", *, filters=None, review_sample=False,
         mixture=None, target_pp=10.0, power=0.8, alpha=0.05, discordance=None):
    delta = target_pp / 100
    if not 0 < delta < 1 or not 0 < alpha / 2 < 0.5 or not 0.5 < power < 1:
        raise ValueError("Use target uplift between 0 and 100 pp, alpha between 0 and 1, and power above 0.5 and below 1")
    if discordance is not None and not delta <= discordance <= 1:
        raise ValueError("Planning discordance must be at least the target uplift fraction and at most 1")
    filters = filters or {}
    if set(filters) - set(DIMENSIONS):
        raise ValueError("Invalid pilot filters")
    comparison = compare(baseline, candidate, metric, filters=filters, review_sample=review_sample)
    left_manifest, left = load_arm(baseline)
    right_manifest, right = load_arm(candidate)
    for field in ("source_sha256", "bank_prompt_version", "policy_sha256"):
        if left_manifest.get(field) != right_manifest.get(field):
            raise ValueError(f"Sizing requires matching {field} across pilot arms")
    if comparison["split"] != "development":
        raise ValueError("Plan from a development pilot and reserve a fresh holdout for confirmation")
    if any(repeat != 1 for _, repeat in left):
        raise ValueError("Sizing requires one attempt per world in each pilot arm")
    selected = {key: row for key, row in left.items()
                if matches(row, filters) and (not review_sample or row["review_sample"])}
    designs = groups_for(selected, mixture)
    selected_keys = [key for _, _, _, keys in designs for key in keys]
    seeds = [left[key]["world_seed"] for key in selected_keys]
    if len(set(seeds)) != len(seeds):
        raise ValueError("Pilot cases share a world seed. Select one case per base world or use cluster-aware sizing.")
    single_family = len({left[key]["family"] for key in selected_keys}) == 1
    iid = single_family and len(designs) == 1
    manifests = (left_manifest, right_manifest)
    groups, prices = zip(*(summarize_group(*design, left, right, manifests, metric) for design in designs))
    costs_complete = all(len(values) == group["worlds"] for values, group in zip(prices, groups))
    pairs = len(seeds)
    complete = sum(group["complete_pairs"] for group in groups)
    weights = [group["weight"] for group in groups]
    possible = [sum(weight * group["possible_uplift"][end] for weight, group in zip(weights, groups))
                for end in (0, 1)]
    radius = math.sqrt(2 * math.log(40) * sum(group["weight"] ** 2 / group["worlds"] for group in groups))
    result = {"metric": metric, "baseline": comparison["baseline"], "candidate": comparison["candidate"],
              "casebook_id": comparison["casebook_id"], "customer_mode": comparison["customer_mode"],
              "score_version": comparison["score_version"], "filters": filters, "review_sample": review_sample,
              "worlds": pairs, "complete_pairs": complete, "excluded_by_mixture": len(selected) - pairs,
              "groups": list(groups), "paired_uplift_pp": None, "baseline_rate": None, "candidate_rate": None,
              "possible_uplift_pp": [100 * value for value in possible],
              "paired_hoeffding_ci95_pp": [100 * max(-1, possible[0] - radius),
                                          100 * min(1, possible[1] + radius)],
              "observed_discordance": None, "discordance_wilson_upper95": None, "headroom_pp": None,
              "headroom_wilson_ci95_pp": None, "planning_discordance": None, "planning_discordance_source": None,
              "max_discordance_at_observed_baseline": None,
              "target_pp": target_pp, "target_power": power, "alpha": alpha,
              "holdout_sampling": "iid_uniform_case_mix" if iid else "fixed_or_mixed",
              "holdout": None}
    if all(group["complete_pairs"] for group in groups):
        # Use simultaneous bounds within each measure when combining a weighted pilot mix.
        normal = NormalDist()
        upper_z = normal.inv_cdf(1 - 0.05 / len(groups))
        interval_z = normal.inv_cdf(1 - 0.025 / len(groups))
        baseline_rate = candidate_rate = observed_q = upper_q = 0.0
        baseline_interval = [0.0, 0.0]
        for group in groups:
            weight, n = group["weight"], group["complete_pairs"]
            tally = group["transitions"]
            base_pass = tally["both_pass"] + tally["loss"]
            cand_pass = tally["both_pass"] + tally["win"]
            disagreements = tally["win"] + tally["loss"]
            baseline_rate += weight * base_pass / n
            candidate_rate += weight * cand_pass / n
            observed_q += weight * disagreements / n
            upper_q += weight * wilson(disagreements, n, upper_z)[1]
            bounds = wilson(base_pass, n, interval_z)
            for end in (0, 1):
                baseline_interval[end] += weight * bounds[end]
        result.update(baseline_rate=baseline_rate, candidate_rate=candidate_rate,
                      paired_uplift_pp=100 * (candidate_rate - baseline_rate), observed_discordance=observed_q,
                      discordance_wilson_upper95=upper_q, headroom_pp=100 * (1 - baseline_rate),
                      headroom_wilson_ci95_pp=[100 * (1 - value) for value in reversed(baseline_interval)])
        if baseline_rate + delta <= 1 + 1e-12:
            result["max_discordance_at_observed_baseline"] = min(2 * (1 - baseline_rate) - delta,
                                                                  2 * baseline_rate + delta)

    # A target needs at least delta discordance. When a small estimate implies perfect directional
    # agreement, the default uses 2*delta to allow regressions under the planning alternative.
    upper_q = result["discordance_wilson_upper95"]
    if discordance is not None:
        planning_q, source = discordance, "specified"
    elif upper_q is None:
        planning_q, source = None, None
    elif upper_q > delta:
        planning_q, source = upper_q, "pilot_wilson_upper95"
    else:
        planning_q, source = min(1, 2 * delta), "sensitivity_at_twice_target"
    result["planning_discordance"] = planning_q
    result["planning_discordance_source"] = source
    options = {value for value in (delta, 0.15, 0.2, 0.3, 0.5) if value >= delta}
    if planning_q is not None:
        options.add(planning_q)
    sizes = {q: planning_size(delta, q, alpha, power, iid) for q in sorted(options)}
    result["sensitivity"] = [{"discordance": q, "worlds": worlds, "method": method}
                             for q, (worlds, method) in sizes.items()]
    methods = {method for _, method in sizes.values()}
    if EXACT_METHOD in methods:
        method = ("Exact unconditional power of the two-sided McNemar test for one attempt per arm and iid worlds "
                  "drawn uniformly across the selected outcome, difficulty and customer behaviour cells.")
    else:
        method = ("Two-sided paired McNemar normal approximation with continuity correction for one attempt "
                  "per arm and independent worlds from the specified mix.")
    if LARGE_SAMPLE_METHOD in methods:
        method += f" The normal approximation is used when exact sizing would require a search beyond {EXACT_SEARCH_LIMIT:,} worlds."
    result["method"] = (f"{method} The power estimate tests zero uplift when the true uplift is the target, "
                        "conditional on the planning discordance and population mix.")
    holdout_sizes = None
    if complete < pairs:
        result["status"] = "incomplete_pilot"
        result["next_step"] = "Resolve missing endpoint scores under the same review and retry rules in both arms."
    elif result["headroom_pp"] + 1e-9 < target_pp:
        result["status"] = "insufficient_observed_headroom"
        result["next_step"] = "Pilot a different scenario mix with enough baseline failures to accommodate the target."
    else:
        worlds, method = sizes[planning_q]
        n = max(worlds, math.ceil(1 / min(weights)))
        allocation = holdout_allocation(groups, n)
        cost = dict.fromkeys(("baseline", "candidate", "total"))
        if costs_complete:
            holdout_sizes = [allocation[group["name"]] for group in groups]
            for arm in ("baseline", "candidate"):
                cost[arm] = math.fsum(size * group["model_cost"][arm]["usd_per_attempt"]
                                      for size, group in zip(holdout_sizes, groups))
            cost["total"] = cost["baseline"] + cost["candidate"]
        result["status"] = "sized"
        result["holdout"] = {"worlds": n, "conversations": 2 * n, "worlds_by_group": allocation,
                             "method": method, "estimated_model_cost_usd": cost}
        result["next_step"] = ("Freeze the planned mix with fresh seeds using --sampling iid and run each world once per arm."
                               if iid else "Freeze the planned mix with fresh seeds and run each world once per arm.")
    result["paired_model_cost_usd"], budget_interval = paired_cost_summary(prices, weights, holdout_sizes)
    if result["holdout"] is not None:
        result["holdout"]["estimated_model_cost_ci95_usd"] = budget_interval
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--metric", choices=METRICS, default="operational_correct")
    parser.add_argument("--target-pp", type=float, default=10)
    parser.add_argument("--power", type=float, default=0.8)
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--discordance", type=float, help="Override the planned fraction of discordant pairs")
    parser.add_argument("--mix", type=Path, help="JSON file of disjoint groups with name, weight and filters")
    parser.add_argument("--review-sample", action="store_true")
    parser.add_argument("--output", type=Path)
    for dimension in DIMENSIONS:
        parser.add_argument("--" + dimension)
    args = parser.parse_args()
    filters = {key: getattr(args, key) for key in DIMENSIONS if getattr(args, key)}
    try:
        mixture = json.loads(args.mix.read_text()) if args.mix is not None else None
        result = plan(args.baseline, args.candidate, args.metric, filters=filters, review_sample=args.review_sample,
                      mixture=mixture, target_pp=args.target_pp, power=args.power, alpha=args.alpha,
                      discordance=args.discordance)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    output = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(output)
    print(output, end="")


if __name__ == "__main__":
    main()
