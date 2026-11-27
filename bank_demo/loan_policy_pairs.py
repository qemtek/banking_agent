"""Freeze loan estimates that differ only in the effective date of a tariff edition."""
import argparse
import json
import random
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

from evaluation.casebooks import digest, validate_contents
from .instances import BEHAVIOURS, VERSION
from .servicing_worlds import servicing_messages, servicing_world


DESIGN = "loan-fee-effective-date-v1"


def generate(seed, pairs, *, general_enquiry=False):
    if pairs < 1:
        raise ValueError("At least one matched pair is required")
    cases = []
    for index in range(pairs):
        world_seed = 4 * int(digest([DESIGN, seed, index])[:15], 16) + 2 * (index % 2)
        world = servicing_world(world_seed, "loan_service", "settlement_fee_estimate", "hard")
        if general_enquiry:
            world["prompt"] = "What would I pay to settle my Home improvement loan today? Please send me the estimate."
        for side, days in (("effective_today", 0), ("effective_tomorrow", 1)):
            twin = deepcopy(world)
            revision = max(twin["policy_editions"], key=lambda edition: edition["effective_from"])
            revision["effective_from"] = str(date.fromisoformat(twin["as_of"]) + timedelta(days=days))
            case = dict(family="loan_service", variant="settlement_fee_estimate", difficulty="hard",
                        behaviour=BEHAVIOURS[index % len(BEHAVIOURS)], split="development",
                        world_seed=world_seed, sibling=side, world=twin)
            case["messages"] = servicing_messages(case)
            cases.append({**case, "case_id": digest([VERSION, case])})
    random.Random(digest([DESIGN, seed, "order"])).shuffle(cases)
    book = dict(design=DESIGN, version=VERSION, seed=seed, split="development", cases=cases)
    validate_contents(book)
    return book


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--pairs", default=8, type=int)
    parser.add_argument("--general-enquiry", action="store_true",
                        help="Use the same loan worlds with a customer enquiry that omits the fee-relief cue")
    args = parser.parse_args()
    book = generate(args.seed, args.pairs, general_enquiry=args.general_enquiry)
    args.file.parent.mkdir(parents=True, exist_ok=True)
    with args.file.open("x", encoding="utf-8") as output:
        json.dump(book, output, indent=2)
        output.write("\n")
    print(f"{args.pairs} matched policy pairs · {digest(book)} · {args.file}")
