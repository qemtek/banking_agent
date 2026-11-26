import json
import math
import tempfile
import unittest
from itertools import islice
from pathlib import Path

from bank_demo.instances import generate
from evaluation.casebooks import digest
from evaluation.power import exact_mcnemar_powers, exact_paired_sample_size, paired_sample_size, plan
from evaluation.uplift import exact_p


class PowerTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.directory = Path(workspace.name)

    def save_arm(self, name, book, statuses, *, source="bank-v1", costs=None):
        directory = self.directory / name
        directory.mkdir()
        manifest = {"suite": "generated", "arm": name, "casebook_id": digest(book), "split": book["split"],
                    "generator_version": book["version"], "score_version": 5, "model": "terra",
                    "reasoning_effort": "medium", "customer_mode": "scripted", "actor_version": 1,
                    "repeats": 1, "source_sha256": source}
        rows = []
        costs = [0.02] * len(statuses) if costs is None else costs
        for case, status, cost in zip(book["cases"], statuses, costs):
            row = {key: case[key] for key in ("case_id", "world_seed", "family", "variant", "behaviour", "difficulty")}
            rows.append({**row, "status": status, "checks": [], "repeat": 1,
                         "estimated_cost_usd": {"total": cost} if cost is not None else {}})
        for filename, data in (("casebook", book), ("manifest", manifest), ("report", rows)):
            (directory / f"{filename}.json").write_text(json.dumps(data))
        return directory

    def test_weighted_design_uses_declared_mix_and_sizes_both_arms(self):
        book = generate(8, "development", ["lookup", "availability"], ["cooperative"], ["easy"], 4)
        base = ["PASS" if case["family"] == "lookup" else "FAIL" for case in book["cases"]]
        cand = ["PASS"] * 8
        mixture = [{"name": "balance", "filters": {"family": "lookup"}, "weight": 0.75},
                   {"name": "outage", "filters": {"family": "availability"}, "weight": 0.25}]
        result = plan(self.save_arm("baseline", book, base), self.save_arm("candidate", book, cand),
                      "task_pass", mixture=mixture, discordance=0.3)
        self.assertEqual(result["status"], "sized")
        self.assertEqual(result["baseline_rate"], 0.75)
        self.assertEqual(result["paired_uplift_pp"], 25)
        self.assertEqual(result["observed_discordance"], 0.25)
        self.assertIn("normal approximation", result["method"])
        holdout = result["holdout"]
        self.assertEqual(holdout["worlds"], paired_sample_size(0.1, 0.3, 0.05, 0.8))
        self.assertEqual(holdout["worlds"], sum(holdout["worlds_by_group"].values()))
        self.assertAlmostEqual(holdout["worlds_by_group"]["balance"] / holdout["worlds"], 0.75, places=2)
        self.assertAlmostEqual(holdout["estimated_model_cost_usd"]["total"], holdout["conversations"] * 0.02)

    def test_single_family_uses_exact_two_sided_sizes(self):
        book = generate(2, "development", ["lookup"], ["cooperative"], ["easy"], 30)
        left = self.save_arm("baseline", book, ["FAIL"] * 10 + ["PASS"] * 20)
        right = self.save_arm("candidate", book, ["FAIL"] * 7 + ["PASS"] * 23)
        result = plan(left, right, "task_pass", filters={"family": "lookup"}, discordance=0.2)
        self.assertIn("Exact unconditional power", result["method"])
        self.assertEqual(result["holdout_sampling"], "iid_uniform_case_mix")
        self.assertIn("--sampling iid", result["next_step"])
        self.assertEqual(result["holdout"]["worlds"], 168)
        self.assertEqual(result["holdout"]["conversations"], 336)
        sizes = {row["discordance"]: row["worlds"] for row in result["sensitivity"]}
        for discordance, worlds in ((0.15, 122), (0.2, 168), (0.3, 249)):
            self.assertEqual(sizes[discordance], worlds)

    def test_cost_bootstrap_resamples_whole_pairs_and_fixes_holdout_count(self):
        book = generate(5, "development", ["lookup"], ["cooperative"], ["easy"], 4)
        left = self.save_arm("baseline", book, ["FAIL", "FAIL", "PASS", "PASS"], costs=[0.01, 0.09, 0.02, 0.08])
        right = self.save_arm("candidate", book, ["PASS"] * 4, costs=[0.09, 0.01, 0.18, 0.12])
        result = plan(left, right, "task_pass", discordance=0.2)
        cost = result["paired_model_cost_usd"]
        self.assertEqual(cost["fully_priced_pairs"], 4)
        self.assertAlmostEqual(cost["mean_per_pair"], 0.15)
        lo, hi = cost["mean_per_pair_ci95"]
        self.assertAlmostEqual(lo, 0.1)
        self.assertAlmostEqual(hi, 0.2)
        holdout = result["holdout"]
        for estimate, per_pair in zip(holdout["estimated_model_cost_ci95_usd"], (lo, hi)):
            self.assertAlmostEqual(estimate, per_pair * holdout["worlds"])
        repeated = plan(left, right, "task_pass", discordance=0.2)
        self.assertEqual(result["paired_model_cost_usd"], repeated["paired_model_cost_usd"])
        self.assertEqual(holdout["estimated_model_cost_ci95_usd"], repeated["holdout"]["estimated_model_cost_ci95_usd"])

    def test_missing_price_keeps_complete_pair_statistics_and_suppresses_budget(self):
        book = generate(5, "development", ["lookup"], ["cooperative"], ["easy"], 4)
        left = self.save_arm("baseline", book, ["FAIL", "FAIL", "PASS", "PASS"], costs=[0.01, 0.02, None, 0.03])
        right = self.save_arm("candidate", book, ["PASS"] * 4)
        result = plan(left, right, "task_pass", discordance=0.2)
        cost = result["paired_model_cost_usd"]
        self.assertEqual(cost["fully_priced_pairs"], 3)
        self.assertAlmostEqual(cost["mean_per_pair"], 0.04)
        self.assertIsNotNone(cost["mean_per_pair_ci95"])
        holdout = result["holdout"]
        self.assertEqual(holdout["estimated_model_cost_usd"], {"baseline": None, "candidate": None, "total": None})
        self.assertIsNone(holdout["estimated_model_cost_ci95_usd"])

    def test_tiny_effect_uses_labeled_large_sample_approximation(self):
        book = generate(2, "development", ["lookup"], ["cooperative"], ["easy"], 4)
        left = self.save_arm("baseline", book, ["FAIL", "FAIL", "PASS", "PASS"])
        right = self.save_arm("candidate", book, ["FAIL", "PASS", "PASS", "PASS"])
        result = plan(left, right, "task_pass", target_pp=0.01, discordance=0.2)
        self.assertIn("normal approximation", result["method"])
        self.assertEqual(result["holdout"]["method"], "normal_approximation_large_sample")
        self.assertEqual(result["holdout"]["worlds"], paired_sample_size(0.0001, 0.2, 0.05, 0.8))

    def test_exact_power_matches_direct_enumeration_and_discrete_cutoffs(self):
        delta, discordance, alpha = 0.2, 0.4, 0.125
        win = (discordance + delta) / 2
        loss = (discordance - delta) / 2
        tie = 1 - discordance
        for pairs, actual in islice(exact_mcnemar_powers(delta, discordance, alpha), 14):
            likelihood = 0
            for wins in range(pairs + 1):
                for losses in range(pairs - wins + 1):
                    if exact_p(wins, losses) <= alpha:
                        ways = math.comb(pairs, wins) * math.comb(pairs - wins, losses)
                        likelihood += ways * win ** wins * loss ** losses * tie ** (pairs - wins - losses)
            self.assertAlmostEqual(actual, likelihood)
        # With no ties the rejection boundary can move farther away on the next world.
        size = exact_paired_sample_size(0.1, 1, 0.05, 0.8)
        powers = dict(islice(exact_mcnemar_powers(0.1, 1, 0.05), size + 1))
        self.assertEqual(size, 786)
        self.assertGreaterEqual(powers[size], 0.8)
        self.assertLess(powers[size + 1], 0.8)

    def test_low_discordance_does_not_report_zero_uncertainty_or_hide_ceiling(self):
        book = generate(2, "development", ["lookup"], ["cooperative"], ["easy"], 30)
        left = self.save_arm("baseline", book, ["PASS"] * 30)
        right = self.save_arm("candidate", book, ["PASS"] * 30)
        result = plan(left, right, "task_pass")
        self.assertEqual(result["observed_discordance"], 0)
        self.assertGreater(result["discordance_wilson_upper95"], 0)
        self.assertEqual(result["planning_discordance"], 0.2)
        self.assertEqual(result["headroom_pp"], 0)
        self.assertEqual(result["status"], "insufficient_observed_headroom")
        self.assertIsNone(result["holdout"])
        self.assertGreater(result["sensitivity"][-1]["worlds"], result["sensitivity"][0]["worlds"])

    def test_missing_endpoint_has_bounds_and_withholds_holdout_size(self):
        book = generate(2, "development", ["lookup"], ["cooperative"], ["easy"], 3)
        left = self.save_arm("baseline", book, ["FAIL", "PASS", "ERROR"])
        right = self.save_arm("candidate", book, ["PASS", "REVIEW", "PASS"])
        result = plan(left, right, "task_pass")
        self.assertEqual(result["groups"][0]["missing_scores"], {"baseline": 1, "candidate": 1})
        self.assertEqual(result["complete_pairs"], 1)
        self.assertAlmostEqual(result["possible_uplift_pp"][1], 200 / 3)
        self.assertEqual(result["possible_uplift_pp"][0], 0)
        self.assertEqual(result["status"], "incomplete_pilot")
        self.assertIsNone(result["holdout"])

    def test_shared_base_world_and_runtime_mismatch_are_rejected(self):
        book = generate(3, "development", ["lookup"], ["cooperative"], ["easy"], 2)
        case = book["cases"][0]
        case["world_seed"] = book["cases"][1]["world_seed"]
        del case["case_id"]
        case["case_id"] = digest([book["version"], case])
        left = self.save_arm("baseline", book, ["FAIL", "PASS"])
        right = self.save_arm("candidate", book, ["FAIL", "PASS"])
        with self.assertRaisesRegex(ValueError, "share a world seed"):
            plan(left, right, "task_pass")
        changed = self.save_arm("changed", book, ["FAIL", "PASS"], source="bank-v2")
        with self.assertRaisesRegex(ValueError, "source_sha256"):
            plan(left, changed, "task_pass")

    def test_replays_must_share_the_same_grading_source(self):
        book = generate(3, "development", ["lookup"], ["cooperative"], ["easy"], 2)
        a = self.save_arm("baseline", book, ["PASS", "FAIL"])
        b = self.save_arm("candidate", book, ["PASS", "PASS"])
        for arm, revision in ((a, "first"), (b, "second")):
            rows = json.loads((arm / "report.json").read_text())
            for row in rows:
                row["replay_source_sha256"] = revision
            (arm / "replay.json").write_text(json.dumps(rows))
        with self.assertRaisesRegex(ValueError, "replay_source_sha256"):
            plan(a / "replay.json", b / "replay.json", "task_pass")
