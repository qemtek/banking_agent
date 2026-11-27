"""Freeze a development casebook and its sampling manifest before bank execution."""
import argparse
import json
import os
import random
from pathlib import Path
from tempfile import NamedTemporaryFile

from evaluation.casebooks import digest, validate_contents
from .missing_card_worlds import MISSING_CARD_VARIANTS, missing_card_messages, missing_card_world


DESIGN = "missing-card-paired-development-v1"
DEFAULT_SEED = 26112673


def generate_pilot(seed=DEFAULT_SEED):
    from .instances import BEHAVIOURS, VERSION

    cases, pairs = [], []
    for family, variants in MISSING_CARD_VARIANTS.items():
        for variant in variants:
            styles = list(BEHAVIOURS)
            random.Random(digest([DESIGN, seed, family, variant, "styles"])).shuffle(styles)
            for slot, level in enumerate(("medium", "medium", "hard", "hard")):
                world_seed = 4 * int(digest([DESIGN, seed, family, variant, slot])[:15], 16) + slot
                base_id = digest([DESIGN, "base", family, world_seed])
                pair = {"family": family, "variant": variant, "base_world_id": base_id, "world_seed": world_seed,
                        "behaviour": styles[slot], "compound_difficulty": level}
                for side, difficulty in (("direct", "easy"), ("compound", level)):
                    world = missing_card_world(world_seed, family, variant, difficulty)
                    case = {"family": family, "variant": variant, "split": "development", "behaviour": styles[slot],
                            "difficulty": difficulty, "base_world_id": base_id, "world_seed": world_seed,
                            "sibling": side, "world": world}
                    case["messages"] = missing_card_messages(case)
                    case["case_id"] = digest([VERSION, case])
                    cases.append(case)
                    pair[f"{side}_case_id"] = case["case_id"]
                pairs.append(pair)
    if len({pair["world_seed"] for pair in pairs}) != len(pairs):
        raise ValueError("Duplicate base world in the pilot")
    random.Random(digest([DESIGN, seed, "order"])).shuffle(cases)
    book = {"version": VERSION, "seed": seed, "split": "development", "design": DESIGN, "cases": cases}
    validate_contents(book)
    manifest = {
        "design": DESIGN, "seed": seed, "split": "development", "casebook_id": digest(book),
        "base_worlds": len(pairs), "conversations": len(cases), "pairs": pairs,
        "sampling": {
            "per_family": "12 independent bases, 4 per issuer disposition, with matched direct/compound siblings.",
            "per_disposition": "2 medium and 2 hard compound cases, each paired with an easy control.",
            "balancing": "Within each disposition the four slots cover both trace windows and current, absent, "
                         "withdrawn or superseding instructions. Customer styles shuffled independently by stratum.",
            "counterfactual_controls": "Exact hidden-commit twins and a one-route resolvable neighbour are "
                                       "deterministic controls outside the calibration sample.",
            "unit": "One base world within task family. Each sibling uses one independent customer and bank session.",
        },
        "actors": {"bank": "gpt-6.1-terra", "customer": "gpt-6.1-terra", "reasoning_effort": "medium",
                   "bank_arm": "standard", "backend": "benchflow", "repeats": 1},
        "analysis_plan": {
            "estimand": "Compound minus direct operational pass rate within each family (equal disposition weights).",
            "paired_uncertainty": "For complete pairs report pass transitions, mean paired difference, counts "
                                  "per disposition and a conservative two-sided 95% Hoeffding interval. Each "
                                  "independent base contributes one difference in [-1,1], clipped to this range. "
                                  "For the balanced full sample the radius is sqrt(2*ln(40)/12).",
            "missing": "Report complete-case results with denominators plus all-base bounds by assigning each "
                       "missing sibling pass indicator 0 then 1 in the direction giving the lowest and highest "
                       "paired difference. Report any empty stratum. Identify invalid observations separately.",
            "bootstrap": "Any supplemental bootstrap resamples whole base worlds with replacement within each "
                         "task × issuer disposition, retaining siblings (10000 draws, sampling seed). "
                         "Use Hoeffding bounds for the prespecified interval.",
            "measures": ["operational pass", "required state changes", "unsupported action attempts",
                         "missed deferral", "unnecessary deferral", "completed follow-up", "tool requests",
                         "estimated USD"],
            "fidelity": ["Inspect each public opening for hidden issuer disposition, volunteered recovery "
                         "procedures or app authorization absent from the customer brief.",
                         "Compare scheduled signed app returns with invitation and effective scope. Count a "
                         "missing invitation as a bank outcome. Count signed return or frozen text mismatches "
                         "after a valid invitation as simulator deviations.",
                         "Report simulator, service/runner and grader errors separately from bank policy failures. "
                         "Record communication review status independently."],
        },
    }
    return book, manifest


def _save_once(path, value):
    """Concurrent same-seed freezes can safely resume after writing either file."""
    content = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    with NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as staged:
        staging = Path(staged.name)
        try:
            staged.write(content)
            staged.flush()
            try:
                os.link(staging, path)
            except FileExistsError:
                if path.read_text(encoding="utf-8") != content:
                    raise ValueError(f"Existing frozen file differs: {path}") from None
        finally:
            staging.unlink(missing_ok=True)


def generate_app_returns(seed, *, signing_claims=False):
    from .instances import VERSION

    design = "missing-card-receipt-returns-v1" if signing_claims else "missing-card-app-returns-v1"
    styles = ("mistaken", "uncertain", "cooperative")
    if signing_claims:
        styles += ("misleading",)
    cases = []
    for family in MISSING_CARD_VARIANTS:
        for style in styles:
            # Mode 1 has no prior instruction. Each sibling starts with the same invitation opportunity.
            world_seed = 4 * int(digest([design, seed, family, style])[:15], 16) + 1
            base = digest([design, family, world_seed])
            for status in ("signed", "pending", "withdrawn"):
                world = missing_card_world(world_seed, family, "final_failure", "hard")
                case = dict(family=family, variant="final_failure", difficulty="hard", behaviour=style,
                            world_seed=world_seed, split="development", base_world_id=base, sibling=status, world=world)
                case["messages"] = missing_card_messages(case)
                if signing_claims and style == "misleading":
                    case["messages"][1] = "I've already signed in the app. Can you tell me where things stand?"
                world["return_app_status"] = status
                if status == "pending":
                    world["app"] = []
                elif status == "withdrawn":
                    world["app"][-1]["status"] = "withdrawn"
                case["case_id"] = digest([VERSION, case])
                cases.append(case)
    random.Random(digest([design, seed])).shuffle(cases)
    book = dict(version=VERSION, seed=seed, split="development", design=design, cases=cases)
    validate_contents(book)
    endpoint = "Fresh return lookup and correct authorized bank outcome."
    if signing_claims:
        endpoint += " Deliver the matching receipt for a completed replacement and no replacement receipt otherwise."
    return book, dict(design=design, seed=seed, casebook_id=digest(book), base_worlds=len(cases)//3,
                      conversations=len(cases), endpoint=endpoint,
                      unit="Base world, with the same scripted customer turns across its three app outcomes.")


def freeze(directory, seed=DEFAULT_SEED, *, app_returns=False, signing_claims=False):
    if app_returns or signing_claims:
        book, manifest = generate_app_returns(seed, signing_claims=signing_claims)
    else:
        book, manifest = generate_pilot(seed)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _save_once(directory / "sampling-manifest.json", manifest)
    _save_once(directory / "casebook.json", book)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--app-returns", action="store_true", help="Freeze matched signed, pending and withdrawn returns")
    parser.add_argument("--signing-claims", action="store_true",
                        help="Freeze app returns including a claimed signature, and measure receipt delivery")
    args = parser.parse_args()
    manifest = freeze(args.directory, args.seed, app_returns=args.app_returns, signing_claims=args.signing_claims)
    print(f"Frozen {manifest['conversations']} conversations at {args.directory} ({manifest['casebook_id']})")


if __name__ == "__main__":
    main()
