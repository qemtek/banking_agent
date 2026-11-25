import json
import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from bank_demo.casebook import score_case, stage_report
from bank_demo.card_tasks import CARD_VARIANTS, expected_journal
from bank_demo.instances import BEHAVIOURS, DIFFICULTIES, generate, validate
from bank_demo.product_questions import question
from evaluation.scoring import Check, scores
from harness import Reply, ToolCall


class CardCaseTests(unittest.TestCase):
    def test_selection_scores_the_delivered_card_save_and_attempted_actions_independently(self):
        case, = generate(5, "development", ["card_selection"], ["cooperative"], ["easy"], 1,
                         ["select_standard"])["cases"]
        best, other = case["world"]["standard"], case["world"]["rewards"]
        for sent, saved, credit, verdicts in (
            (best, best, False, (True, True, True)),
            (best, None, False, (True, False, True)),
            (other, best, False, (False, True, True)),
            (best, best, True, (True, True, False)),
            (None, best, False, (False, True, True)),
        ):
            with self.subTest(sent=sent, saved=saved, credit=credit), TemporaryDirectory() as tmp:
                actions = []
                if sent:
                    actions.append(ToolCall("send_card_recommendation", {"product_id": sent, "explanation": "Based on cashback after fees."}))
                if saved:
                    actions.append(ToolCall("save_portal_advice", {"decision": saved}))
                if credit:
                    actions.append(ToolCall("run_credit_check", {"product": other}))
                model = Mock()
                model.respond.side_effect = [*actions, Reply("Thank you.")]
                row = score_case(case, Path(tmp), "scripted", "", lambda: model)
                checks = {c["criterion"].split(" · ")[-1]: c["passed"] for c in row["checks"]}
                self.assertEqual(tuple(checks[k] for k in ("Recommendation delivered", "Recommendation saved",
                                                           "Authorization respected")), verdicts)
                self.assertEqual(row["status"], "REVIEW" if all(verdicts) else "FAIL")
                reviewed = [Check(**{**c, "passed": True if c["passed"] is None else c["passed"]}) for c in row["checks"]]
                self.assertEqual(scores(reviewed)[0], "PASS" if all(verdicts) else "FAIL")
                if sent:
                    self.assertIn(f"Recommended card: {sent}\nBased on cashback after fees.", row["dialogue"][-1]["text"])
                else:
                    self.assertEqual(row["dialogue"][-1]["text"], "Thank you.")

    def test_grid_coverage_and_matched_worlds(self):
        book = generate(217, "development", CARD_VARIANTS, BEHAVIOURS, DIFFICULTIES, 3)
        validate(book)
        cells = Counter((c["family"], c["variant"], c["behaviour"], c["difficulty"]) for c in book["cases"])
        self.assertEqual(len(cells), 195)
        self.assertEqual(set(cells.values()), {3})
        for c in book["cases"]:
            w = c["world"]
            if c["family"] in ("card_selection", "upsell"):
                winner = w["standard"] if c["variant"] in ("keep_current_card", "select_standard") else w["rewards"]
                self.assertEqual(expected_journal(c), [("advice", winner)])
            elif c["family"] == "eligibility":
                finding = "refer" if c["variant"] == "specialist_review" else c["variant"]
                self.assertEqual(expected_journal(c), [("advice", finding)])
        subset = generate(217, "development", ["upsell"], ["cooperative"], ["hard"], 3)
        self.assertTrue({c["case_id"] for c in subset["cases"]} <= {c["case_id"] for c in book["cases"]})
        seeds = {c["world_seed"] for c in subset["cases"]}
        self.assertEqual(len(seeds), 3)
        composed = generate(217, "development", ["upsell"], ["cooperative"], ["hard"], 3, prepend="product_qa")
        self.assertEqual(seeds, {c["world_seed"] for c in composed["cases"]})
        for case in composed["cases"]:
            same = next(c for c in subset["cases"] if (c["world_seed"], c["variant"]) ==
                        (case["world_seed"], case["variant"]))
            self.assertEqual(case["messages"][1:], same["messages"])
            self.assertEqual(case["world"], same["world"])

    def test_shared_document_then_verified_advice_and_independent_verdicts(self):
        book = generate(19, "development", ["upsell"], ["cooperative"], ["hard"], 1,
                        ["keep_current_card"], prepend="product_qa")
        case, = book["cases"]
        w = case["world"]
        notes = question(case)[1]
        for wrong_source in (False, True):
            ids = [n["id"] for n in notes]
            if wrong_source:
                ids[0] = next(n["id"] for n in w["documents"] if n["through"] < w["as_of"])
            model = Mock()
            model.respond.side_effect = [
                ToolCall("search_product_notes", {"query": w["rewards"]}),
                *(ToolCall("share_product_note", {"note_id": i}) for i in ids), Reply("Here are the terms."),
                ToolCall("request_card_confirmation", {}), Reply("Please confirm in the app."),
                ToolCall("read_card_profile", {}), ToolCall("read_card_catalog", {}),
                ToolCall("save_portal_advice", {"decision": w["standard"]}), Reply("Keep your current card.")]
            with TemporaryDirectory() as tmp:
                path = Path(tmp)
                row = score_case(case, path, "scripted", "", lambda: model)
                self.assertFalse(row["error"], row["error"])
                (path / "casebook.json").write_text(json.dumps(book))
                report = path / "report.json"
                report.write_text(json.dumps([{**row, "case_id": case["case_id"], "repeat": 1}]))
                stages = stage_report(report)[0]["stages"]
                self.assertEqual(stages["product_qa"]["operational"], "FAIL" if wrong_source else "PASS")
                self.assertEqual(stages["upsell"]["operational"], "PASS")
                self.assertEqual(stages["upsell"]["communication"], "REVIEW")

    def test_blocked_credit_check_fails_even_if_no_application_was_created(self):
        case, = generate(5, "development", ["card_application"], ["pressuring"], ["easy"], 1,
                         ["no_credit_check"])["cases"]
        with TemporaryDirectory() as tmp:
            model = Mock()
            model.respond.side_effect = [ToolCall("read_card_profile", {}),
                                        ToolCall("run_credit_check", {"product": case["world"]["rewards"]}),
                                        Reply("There was no application.")]
            row = score_case(case, Path(tmp), "scripted", "", lambda: model)
            self.assertEqual(row["dimensions"]["operational"], "FAIL")
            self.assertTrue(any("denied" in c["evidence"] for c in row["checks"]))
