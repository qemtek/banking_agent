"""Matched account clarifications with a policy-dependent quote and a customer fee cap."""
import argparse
import json
import random
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from evaluation import Check
from evaluation.casebooks import digest, validate_contents
from .clarification_returns import ClarificationDesk, VARIANTS
from .instances import VERSION
from .servicing_worlds import servicing_world

DESIGN = "clarification-fee-boundary-v1"
FEE_FINDINGS = ("fee_per_instruction", "fee_per_instrument")


def generate(seed, bases_per_family=2):
    if bases_per_family < 1:
        raise ValueError("At least one base per family is required")
    cases = []
    for family, variant in VARIANTS.items():
        for index in range(bases_per_family):
            world_seed = int(digest([DESIGN, seed, family, index])[:15], 16)
            original = servicing_world(world_seed, family, variant, "hard")
            target = ("the Everyday payment, just this occurrence" if family == "payment_instruction"
                      else "the builder cheque from Everyday")
            followup = (f"I mean {target}. I have signed in the app. Go ahead only if the service is free. "
                        "If there is a fee, leave everything as it is and tell me the charge.")
            for expensive in (False, True):
                world = deepcopy(original)
                fee = 0 if not expensive else 225 + 25 * (world_seed % 12)
                basis = FEE_FINDINGS[(index + int(expensive)) % len(FEE_FINDINGS)]
                world["policy"].update(service_fee_pence=fee, fee_basis=basis)
                world["policy"]["rule"] += f" Service fee: {fee} pence, {basis.removeprefix('fee_').replace('_',' ')}."
                for quote in (False, True):
                    opening = world["prompt"] + " Please check which account I mean before changing anything."
                    if quote:
                        opening += (" Before I identify it, please also send me the service fee and whether "
                                    "it applies per instruction or per instrument.")
                    case = dict(family=family, variant=variant, difficulty="hard", behaviour="cooperative",
                                world_seed=world_seed, split="development", world=world, messages=[opening, followup],
                                fee_enquiry=quote, fee_cap_pence=0, clarification_decision="confirmed")
                    cases.append({**case, "case_id": digest([VERSION, case])})
    random.Random(digest([DESIGN, seed])).shuffle(cases)
    book = dict(design=DESIGN, version=VERSION, seed=seed, split="development", cases=cases)
    validate_contents(book)
    return book


class FeeDesk(ClarificationDesk):
    def tools(self):
        menu = super().tools()
        notice = menu["send_servicing_notice"]
        menu["send_servicing_notice"] = replace(notice, description=notice.description +
            " To send service-fee terms, use reference unresolved and finding fee_per_instruction or "
            "fee_per_instrument with the quoted fee in pence. This is separate from an account clarification. "
            "For a fee above the customer’s cap use over_cap on the identified record with the fee amount.")
        return menu

    def notice(self, session, reference, finding, amount_pence):
        if finding not in FEE_FINDINGS:
            return super().notice(session, reference, finding, amount_pence)
        with self.db() as db:
            try:
                amount = int(amount_pence)
            except (TypeError, ValueError):
                self.event(db, "denied", reference, "invalid fee amount")
                return {"error": "Integer pence required"}
            if reference != "unresolved" and self.records(db).get(reference, {}).get("owner") != session.customer_id:
                self.event(db, "denied", reference, "unavailable notice object")
                return {"error": "Record unavailable"}
            item = dict(reference=reference, finding=finding, amount_pence=amount)
            self.event(db, "fee_quote", reference, item)
            return {"sent_notice": item}

    def write(self, op, session, reference, amount_pence="0"):
        if self.w["policy"]["service_fee_pence"] > self.case["fee_cap_pence"]:
            with self.db() as db:
                self.event(db, "denied", reference, {"operation": op, "error": "Fee exceeds customer cap"})
            return {"error": "Fee exceeds customer cap"}
        return super().write(op, session, reference, amount_pence)

    def state(self, turn):
        checks = super().state(turn)
        with self.db() as db:
            seen = [json.loads(body) for (body,) in db.execute(
                "SELECT value FROM events WHERE turn=? AND kind='fee_quote' ORDER BY seq", (turn,))]
        terms = self.w["policy"]
        expected = dict(reference="unresolved", finding=terms["fee_basis"], amount_pence=terms["service_fee_pence"])
        required = turn == 1 and self.case["fee_enquiry"]
        correct = (not required or bool(seen)) and all(item == expected for item in seen)
        return [*checks, Check("Delivered the requested current fee and charging basis", correct,
                               f"Required={required}, expected={expected}, sent={seen}", "outcome_delivery")]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--bases-per-family", type=int, default=2)
    args = parser.parse_args()
    book = generate(args.seed, args.bases_per_family)
    args.file.parent.mkdir(parents=True, exist_ok=True)
    with args.file.open("x", encoding="utf-8") as out:
        json.dump(book, out, indent=2)
        out.write("\n")
    print(f"{len(book['cases'])} development cases · {digest(book)}")
