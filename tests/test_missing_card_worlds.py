import json
import unittest
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from bank_demo.missing_card_pilot import DEFAULT_SEED, freeze, generate_app_returns, generate_pilot
from bank_demo.missing_card_worlds import MISSING_CARD_VARIANTS, missing_card_messages, missing_card_world
from evaluation.casebooks import digest, validate_contents


class MissingCardWorldTests(unittest.TestCase):
    def test_claimed_signature_is_independent_of_trusted_decision(self):
        book, _ = generate_app_returns(27, signing_claims=True)
        claims = [c for c in book["cases"] if c["behaviour"] == "misleading"]
        for family in MISSING_CARD_VARIANTS:
            siblings = [c for c in claims if c["family"] == family]
            self.assertEqual(len({tuple(c["messages"]) for c in siblings}), 1)
            self.assertIn("I've already signed", siblings[0]["messages"][1])
            self.assertEqual({c["sibling"]: [e["status"] for e in c["world"]["app"]] for c in siblings},
                             {"pending": [], "withdrawn": ["withdrawn"], "signed": ["signed"]})

    def test_independent_paired_pilot_and_balanced_dispositions(self):
        book, manifest = generate_pilot()
        validate_contents(book)
        self.assertEqual(len(book["cases"]), 48)
        self.assertEqual(len(manifest["pairs"]), 24)
        self.assertEqual(manifest["casebook_id"], digest(book))
        self.assertEqual(len({p["world_seed"] for p in manifest["pairs"]}), 24)
        self.assertEqual(len({p["base_world_id"] for p in manifest["pairs"]}), 24)
        self.assertEqual({c["split"] for c in book["cases"]}, {"development"})
        self.assertEqual(Counter(c["difficulty"] for c in book["cases"]), {"easy": 24, "medium": 12, "hard": 12})
        cases = {c["case_id"]: c for c in book["cases"]}
        changes = {"issuer", "archive", "courier", "history"}
        windows = {}
        consent = {}
        for pair in manifest["pairs"]:
            direct, compound = (cases[pair[f"{side}_case_id"]] for side in ("direct", "compound"))
            key = (pair["family"], pair["variant"])
            self.assertEqual(direct["sibling"], "direct")
            self.assertEqual(compound["sibling"], "compound")
            self.assertEqual(direct["messages"], compound["messages"])
            self.assertEqual(direct["base_world_id"], compound["base_world_id"])
            self.assertEqual(direct["world_seed"], compound["world_seed"])
            left, right = direct["world"], compound["world"]
            self.assertEqual(set(left), set(right))
            self.assertEqual({k for k in left if left[k] != right[k]}, changes)
            self.assertEqual(len(left["issuer"]), 1)
            self.assertGreater(len(right["issuer"]), 1)
            self.assertLess(left["now"], left["followup_by"])
            self.assertNotEqual(left["profile_destination"], left["old_destination"])
            windows.setdefault(key, []).append(left["trace_eligible_on"] <= left["as_of"])
            if pair["variant"] == "final_failure":
                visible = [row["status"] for row in left["app"] if row["turn"] == 1]
                consent.setdefault(key, []).append(tuple(visible))
        for family, dispositions in MISSING_CARD_VARIANTS.items():
            for disposition in dispositions:
                pairs = [p for p in manifest["pairs"] if p["family"] == family and p["variant"] == disposition]
                self.assertEqual(Counter(p["compound_difficulty"] for p in pairs), {"medium": 2, "hard": 2})
                self.assertEqual(len({p["behaviour"] for p in pairs}), 4)
                self.assertEqual(sum(windows[family, disposition]), 2)
            self.assertEqual(set(consent[family, "final_failure"]),
                             {(), ("signed",), ("signed", "withdrawn"), ("signed", "signed")})

    def test_hidden_twins_and_decisive_neighbour(self):
        for seed in range(4):
            for family in MISSING_CARD_VARIANTS:
                lost = missing_card_world(seed, family, "unresolved", "hard", hidden_commit=False)
                existing = missing_card_world(seed, family, "unresolved", "hard", hidden_commit=True)
                neighbour = missing_card_world(seed, family, "dispatched", "hard")
                self.assertEqual({k for k in lost if lost[k] != existing[k]}, {"hidden_commit"})
                delta = {k for k in existing if existing[k] != neighbour[k]}
                self.assertIn(delta, ({"issuer", "disposition"}, {"archive", "disposition"}))
                attempt = lost["attempt"]
                self.assertTrue(all(lost[route][attempt]["status"] in {"unavailable", "no_record_yet"}
                                    for route in ("issuer", "archive")))
                self.assertEqual(lost["courier"][attempt]["status"], "no_scan")
                route = (delta - {"disposition"}).pop()
                confirmation = neighbour[route][attempt]
                self.assertEqual(confirmation["order_id"], lost["order"])
                self.assertEqual(confirmation["destination"], lost["old_destination"])

    def test_fresh_continuation_and_scope_visibility(self):
        for seed in range(32):
            world = missing_card_world(seed, "missing_card_continuation", "final_failure", "hard")
            incoming = world["investigations"][world["case_ref"]]
            self.assertEqual(incoming["status"], "open")
            self.assertEqual(incoming["attempt"], world["attempt"])
            self.assertEqual(incoming["followup_at"], world["now"])
            self.assertEqual(incoming["owner"], world["owner"])
            self.assertEqual(len(world["investigations"]), 2)
            for message in world["archived_dialogue"]:
                self.assertLess(message["at"], world["now"])
            promise = world["archived_dialogue"][1]["content"]
            self.assertIn(world["case_ref"], promise)
            self.assertIn(world["now"], promise)
            events = world["app"]
            self.assertEqual([row["at"] for row in events], sorted(row["at"] for row in events))
            self.assertTrue(all(row["attempt"] == world["attempt"] and row["card"] == world["card"]
                                and row["action"] == "replacement" for row in events))
            self.assertEqual(events[-1]["destination"], world["signed_destination"])
            self.assertEqual(events[-1]["requires_invite"], seed % 4 in (1, 2))
            for event in events:
                if event["turn"] == 1:
                    self.assertLessEqual(event["at"], world["now"])
                else:
                    self.assertGreater(event["at"], world["now"])
                    self.assertEqual(date.fromisoformat(world["as_of"]), datetime.fromisoformat(
                        event["at"].replace("Z", "+00:00")).date())
            first_contact = missing_card_world(seed, "missing_card_contact", "final_failure", "hard")
            self.assertEqual(first_contact["archived_dialogue"], [])
            self.assertEqual(first_contact["investigations"], {})

    def test_customer_styles_do_not_reveal_decision_or_commit(self):
        for family, variants in MISSING_CARD_VARIANTS.items():
            for style in ("cooperative", "misleading", "pressuring", "uncertain", "mistaken"):
                openings = []
                for variant in variants:
                    world = missing_card_world(10, family, variant, "hard")
                    messages = missing_card_messages({"family": family, "variant": variant,
                                                      "behaviour": style, "world": world})
                    openings.append(messages[0])
                    for term in ("issuer", "trace", "replacement", "consent", "investigation", "timeout",
                                 "dispatch", "signed", "tool", world["attempt"], world["case_ref"]):
                        self.assertNotIn(term.lower(), messages[0].lower())
                    self.assertEqual(len(messages), 2 if variant == "final_failure" else 1)
                self.assertEqual(len(set(openings)), 1)

    def test_freeze_is_reproducible_and_preserves_existing_files(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            first = freeze(folder)
            self.assertEqual(first, freeze(folder))
            book = json.loads((folder / "casebook.json").read_text())
            self.assertEqual(digest(book), first["casebook_id"])
            with self.assertRaisesRegex(ValueError, "Existing frozen file differs"):
                freeze(folder, DEFAULT_SEED + 1)
            self.assertEqual(first, json.loads((folder / "sampling-manifest.json").read_text()))
            self.assertEqual(book, json.loads((folder / "casebook.json").read_text()))
        second, _ = generate_pilot(DEFAULT_SEED + 1)
        self.assertTrue({c["world_seed"] for c in book["cases"]}.isdisjoint(c["world_seed"] for c in second["cases"]))

    def test_invalid_counterfactuals_are_rejected(self):
        with self.assertRaises(ValueError):
            missing_card_world(5, "missing_card_contact", "final_failure", "hard", hidden_commit=True)
        with self.assertRaises(ValueError):
            missing_card_world(5, "missing_card_contact", "unresolved", "hard", hidden_commit="false")
