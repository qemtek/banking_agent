"""Freeze development siblings for timeout recovery and long dispute conversations."""

import argparse
import json
import random
from pathlib import Path

from evaluation.casebooks import digest
from .instances import VERSION, public_messages, validate, world
from .card_servicing_tasks import card_servicing_world

TIMEOUT_INSTRUCTION = (
    "When a customer returns after a replacement request timed out, read the saved order before retrying. "
    "If a replacement is recorded, give its issuer receipt. Otherwise check current authority, "
    "complete the authorized replacement once and give its issuer receipt."
)


VERIFICATION_INSTRUCTION = TIMEOUT_INSTRUCTION + (
    " If the customer explicitly asks to check again, inspect the current order on that turn "
    "even if an earlier reply included the receipt. A simple thank-you needs no further lookup."
)


def generate(kind, seed, bases):
    if kind not in ("timeout", "verification", "compaction") or bases < 1:
        raise ValueError("Choose a supported experiment and a positive number of base worlds")
    cases = []
    for index in range(bases):
        world_seed = int(digest([kind, seed, index])[:15], 16)
        common = dict(world_seed=world_seed, difficulty="hard", behaviour="cooperative", split="development")
        if kind in ("timeout", "verification"):
            for variant in ("lost_ack_committed", "lost_ack_uncommitted"):
                w = card_servicing_world(world_seed, "card_reissue", variant, "hard")
                requests = (
                    "The last adviser requested my replacement, but the acknowledgement timed out. "
                    "Is there a replacement already? If it is still needed, please complete my signed request.",
                    "I still don't know whether my returned card has been replaced. The last request timed out. "
                    "I signed for one replacement. Can you sort this out without ordering two?",
                )
                message = f"Order {w['order']}. {requests[index % 2]} Please send the issuer receipt."
                c = dict(**common, family="card_reissue", variant=variant, world=w, timeout_return=True)
                if kind == "verification":
                    for stage, followup in (
                        (
                            "verify_again",
                            "Thank you. Could you check the saved order once more now and confirm the issuer receipt?",
                        ),
                        ("thanks", "Thank you, that answers my question."),
                    ):
                        cases.append(dict(**c, return_followup=stage, messages=[message, followup]))
                else:
                    cases.append(dict(**c, messages=[message]))
        else:
            for variant in ("keep_linked", "conditional_linked", "conditional_unrelated"):
                c = dict(
                    **common,
                    family="followup",
                    variant=variant,
                    world=world(world_seed, "hard", variant),
                    intervening=["receipt", "case_details", "same_value", "credit_link"],
                    credit_posts_on_turn=6,
                )
                c["messages"] = public_messages(c)
                cases.append(c)
    frozen = [{**c, "case_id": digest([VERSION, c])} for c in cases]
    random.Random(digest([kind, seed, "order"])).shuffle(frozen)
    book = dict(design=kind + "-recovery-v1", version=VERSION, split="development", seed=seed, cases=frozen)
    validate(book)
    return book


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("timeout", "verification", "compaction"))
    parser.add_argument("file", type=Path)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--bases", type=int, required=True)
    args = parser.parse_args()
    book = generate(args.kind, args.seed, args.bases)
    args.file.parent.mkdir(parents=True, exist_ok=True)
    with args.file.open("x") as output:
        json.dump(book, output, indent=2)
        output.write("\n")
    print(f"{len(book['cases'])} development cases · {digest(book)}")
