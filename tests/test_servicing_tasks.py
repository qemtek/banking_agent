"""Normal harness controls for all seven servicing families, plus intentional grading failures."""
import tempfile
import unittest
from pathlib import Path

from bank_demo import Session
from bank_demo.loan_policy_pairs import generate as loan_policy_pairs
from bank_demo.servicing_oracle import expected_service
from bank_demo.servicing_tasks import ServicingDesk, servicing_messages, servicing_task
from bank_demo.servicing_worlds import SERVICING_VARIANTS, servicing_world
from evaluation import evaluate
from harness import Reply, ToolCall


def specimen(family, variant, level="easy", seed=429):
    case = {"family": family, "variant": variant, "difficulty": level, "behaviour": "cooperative",
            "world": servicing_world(seed, family, variant, level)}
    case["messages"] = servicing_messages(case)
    return case


def script(case):
    w = case["world"]
    calls = []
    for turn in range(1,len(case["messages"])+1):
        e = expected_service(case, turn)
        calls.extend([ToolCall("read_servicing_policy",{}), ToolCall("list_servicing_records",{})])
        extra = {k for k,r in w["records"].items() if r["owner"] == "alice" and r["kind"] == "account"} if case["family"]=="account_lifecycle" else set()
        for ref in sorted(e.evidence | {w["subject"]} | extra):
            calls.append(ToolCall("read_servicing_record",{"reference": ref}))
        if case["family"] == "atm_fee":
            calls.append(ToolCall("read_atm_statement",{"account_reference":w["primary"]}))
            entries = {k:r for k,r in w["records"].items() if r["kind"]=="withdrawal" and r["account"]==w["primary"]
                       and r["day"][:7]==w["policy"]["period"]}
            fees = sum(r["bank_fee"]-r["reversal"] for r in entries.values())
            due = max(0,sum(r["network"]=="partner" for r in entries.values())-w["policy"]["free"])*w["policy"]["fee"]
            calls.append(ToolCall("send_atm_breakdown",{"account_reference":w["primary"],"charged_pence":str(fees),
                                  "due_pence":str(due),"withdrawal_references":",".join(entries)}))
        if case["variant"] == "settlement_fee_estimate" or w["records"][w["subject"]]["kind"] == "offer" or any(
            op in ("settle_loan","request_settlement","redeem_deposit") for op,_ in w["scope"]
        ):
            calls.append(ToolCall("get_servicing_quote",{"reference":w["subject"]}))
        for op,ref in e.operations:
            args = {"reference":ref}
            if op in ("schedule_payment","amend_series","credit_atm_fees","request_limit"):
                args["amount_pence"] = str(e.amount)
            calls.append(ToolCall(op,args))
        ref = "unresolved" if e.finding == "clarify" else w["subject"]
        calls.extend([ToolCall("send_servicing_notice",{"reference":ref,"finding":e.finding,"amount_pence":str(e.amount)}),
                      Reply("Here are the details you requested.")])
    return calls


class Recorded:
    def __init__(self, calls):
        self.calls = iter(calls)

    def respond(self, messages, tools):
        return next(self.calls)


def run(case, calls):
    with tempfile.TemporaryDirectory() as temp:
        return evaluate(servicing_task(case,lambda: Recorded(calls)),Path(temp))


class ServicingTests(unittest.TestCase):
    def test_future_tariff_changes_the_notice_but_not_the_source_quote(self):
        twins = loan_policy_pairs(42, 1)["cases"]
        left, right = twins
        self.assertEqual(left["world"]["records"], right["world"]["records"])
        self.assertEqual(left["messages"], right["messages"])
        edits = [key for a,b in zip(left["world"]["policy_editions"], right["world"]["policy_editions"])
                 for key in a if a[key] != b[key]]
        self.assertEqual(edits, ["effective_from"])
        for case, other in ((left, right), (right, left)):
            correct = expected_service(case, 1).amount
            incorrect = expected_service(other, 1).amount
            self.assertNotEqual(correct, incorrect)
            actions = script(case)
            notice = next(i for i,a in enumerate(actions) if isinstance(a, ToolCall) and a.name == "send_servicing_notice")
            actions[notice] = ToolCall("send_servicing_notice", {**actions[notice].arguments, "amount_pence": str(incorrect)})
            result = run(case, actions)
            self.assertTrue(all(c.passed for c in result.checks if c.category == "evidence"))
            self.assertEqual([c.category for c in result.checks if c.passed is False], ["outcome_delivery"])

    def test_all_counterfactuals_and_levels(self):
        for family, variants in SERVICING_VARIANTS.items():
            for variant in variants:
                for level in ("easy","medium","hard"):
                    with self.subTest(family=family, variant=variant, level=level):
                        case = specimen(family,variant,level)
                        outcome = run(case,script(case))
                        self.assertEqual("PASS", outcome.dimensions.get("operational"),
                                         (outcome.error, [c for c in outcome.checks if c.passed is False]))

    def test_payment_wrong_record_and_invented_success(self):
        case = specimen("payment_instruction","skip_occurrence")
        calls = script(case)
        i = next(i for i,c in enumerate(calls) if isinstance(c,ToolCall) and c.name=="skip_payment")
        other = next(ref for ref,r in case["world"]["records"].items() if r["kind"]=="payment" and ref!=case["world"]["subject"])
        for replacement in ([],[ToolCall("skip_payment",{"reference":other})]):
            outcome = run(case,calls[:i]+replacement+calls[i+1:])
            self.assertEqual(outcome.dimensions["operational"],"FAIL")
            self.assertTrue(any(not c.passed for c in outcome.checks if c.category=="bank_writes"))

    def test_required_quote_and_reread(self):
        case = specimen("loan_service","settlement_over_cap","hard")
        calls = script(case)
        for tool in ("get_servicing_quote","read_servicing_record"):
            i = max(i for i,c in enumerate(calls) if isinstance(c,ToolCall) and c.name==tool
                    and c.arguments["reference"]==case["world"]["subject"])
            out = run(case,calls[:i]+calls[i+1:])
            self.assertEqual("FAIL",out.dimensions["operational"])

    def test_wrong_order_refusal_and_collateral_state(self):
        case = specimen("dispute_credit_limit","limit_then_dispute")
        calls = script(case)
        indices = [i for i,c in enumerate(calls) if isinstance(c,ToolCall) and c.name in ("request_limit","file_dispute")]
        a,b = indices
        calls[a],calls[b] = calls[b],calls[a]
        self.assertEqual("FAIL",run(case,calls).dimensions["operational"])
        case = specimen("dispute_credit_limit","dispute_only")
        calls = script(case)
        pos = next(i for i,c in enumerate(calls) if isinstance(c,ToolCall) and c.name=="file_dispute")
        card = next(ref for ref,r in case["world"]["records"].items() if r["kind"]=="card" and r["label"]=="Travel")
        calls.insert(pos,ToolCall("request_limit",{"reference":card,"amount_pence":str(case["world"]["policy"]["requested_limit"])}))
        self.assertEqual("FAIL",run(case,calls).dimensions["operational"])
        with tempfile.TemporaryDirectory() as temp:
            desk = ServicingDesk(Path(temp),case)
            desk.advance(1)
            with desk.db() as db:
                db.execute("UPDATE records SET body=json_set(body,'$.available',0) WHERE json_extract(body,'$.owner')='bob'")
            self.assertFalse(desk.state(1)[0].passed)

    def test_independent_requests_accept_either_order(self):
        for family,variant,ops in (("account_lifecycle","either_order",("open_savings","close_current")),
                                   ("dispute_credit_limit","independent_cards",("file_dispute","request_limit"))):
            case = specimen(family,variant)
            calls = script(case)
            a,b = [i for i,c in enumerate(calls) if isinstance(c,ToolCall) and c.name in ops]
            calls[a],calls[b] = calls[b],calls[a]
            self.assertEqual("PASS",run(case,calls).dimensions["operational"])

    def test_missing_evidence_notice_and_wrong_monetary_amount(self):
        for family,variant in (("atm_fee","credit_fees"),("term_deposit","book_recurring"),
                               ("cheque_service","stop_cheque"),("account_lifecycle","close_current")):
            case = specimen(family,variant)
            calls = script(case)
            final_notice = max(i for i,c in enumerate(calls) if isinstance(c,ToolCall) and c.name=="send_servicing_notice")
            original = calls[final_notice]
            calls[final_notice] = ToolCall(original.name,{**original.arguments,"amount_pence":str(int(original.arguments["amount_pence"])+1)})
            self.assertEqual("FAIL",run(case,calls).dimensions["operational"])
            calls = [c for c in script(case) if not isinstance(c,ToolCall) or c.name != "read_servicing_policy"]
            self.assertEqual("FAIL",run(case,calls).dimensions["operational"])

    def test_financial_fixtures_have_independent_numeric_controls(self):
        from bank_demo.servicing_rules import maturity
        from bank_demo.servicing_worlds import monthly_payment

        fixed = {"principal": 200000, "rate_bp": 400, "months": 12, "type": "fixed"}
        self.assertEqual(maturity(fixed), 208000)
        recurring = {"principal": 10000, "rate_bp": 1200, "months": 12, "type": "recurring"}
        self.assertEqual(maturity(recurring), 127800)
        self.assertEqual(monthly_payment(120000, 1200, 12), 10662)
        self.assertEqual(monthly_payment(120001, 0, 12), 10000)

    def test_alternative_eligibility_evidence_and_private_mismatch(self):
        case = specimen("account_lifecycle", "open_savings", "medium")
        calls = [c for c in script(case) if not isinstance(c, ToolCall) or c.name != "read_servicing_record"
                 or c.arguments["reference"] in (case["world"]["subject"], case["world"]["primary"])]
        self.assertEqual("PASS", run(case,calls).dimensions["operational"])
        ineligible = specimen("account_lifecycle", "tenure_blocked", "medium")
        calls = [c for c in script(ineligible) if not isinstance(c, ToolCall) or c.name != "read_servicing_record"
                 or c.arguments["reference"] != ineligible["world"]["other"]]
        self.assertEqual("FAIL", run(ineligible,calls).dimensions["operational"])

    def test_atm_reconciliation_rejects_right_total_with_wrong_entries(self):
        case = specimen("atm_fee", "credit_fees", "hard")
        calls = script(case)
        prev = next(k for k,r in case["world"]["records"].items() if r["label"] == "Prior period ATM")
        for i,c in enumerate(calls):
            if isinstance(c,ToolCall) and c.name == "send_atm_breakdown":
                args = dict(c.arguments)
                refs = args["withdrawal_references"].split(",")
                refs[0] = prev
                args["withdrawal_references"] = ",".join(refs)
                calls[i] = ToolCall(c.name,args)
        out = run(case,calls)
        self.assertEqual("FAIL",out.dimensions["operational"])
        self.assertTrue(all(c.passed for c in out.checks if c.category == "bank_writes"))

    def test_manual_payment_skip_trace_and_rendered_receipt(self):
        case = specimen("payment_instruction", "skip_occurrence")
        w = case["world"]
        ref = next(k for k,r in w["records"].items() if r.get("due") and r["due"] in case["messages"][0] and r["kind"]=="payment")
        args = {"reference":ref,"amount_pence":str(w["records"][ref]["amount"])}
        calls = [ToolCall("read_servicing_policy",{}),ToolCall("list_servicing_records",{}),
                 ToolCall("send_servicing_notice",{**args,"finding":"estimate"}), Reply("I will wait for your confirmation."),
                 ToolCall("skip_payment",{"reference":ref}), ToolCall("send_servicing_notice",{**args,"finding":"skipped"}),
                 Reply("The later payments are still scheduled.")]
        out=run(case,calls)
        self.assertEqual("PASS",out.dimensions["operational"])
        self.assertIn("Bank receipt RC-",out.dialogue[-1]["text"])
        self.assertIn(ref,out.dialogue[-1]["text"])

    def test_trusted_app_permissions_do_not_expose_future_or_other_customer_actions(self):
        case = specimen("dispute_credit_limit","dispute_only")
        self.assertIn("My app confirmation is complete.",case["messages"][1])
        default = specimen("payment_instruction","cancel_series")
        self.assertEqual(1,default["messages"][1].count("app"))
        with tempfile.TemporaryDirectory() as temp:
            desk = ServicingDesk(Path(temp),case)
            desk.advance(1)
            self.assertEqual([],desk.permissions(Session("alice"))["confirmed_actions"])
            desk.advance(2)
            ref = case["world"]["subject"]
            self.assertEqual([{"operation":"file_dispute","reference":ref}],desk.permissions(Session("alice"))["confirmed_actions"])
            self.assertEqual([],desk.permissions(Session("bob"))["confirmed_actions"])
            self.assertIn("error",desk.write("file_dispute",Session("bob"),ref))
            travel = next(k for k,r in case["world"]["records"].items() if r["label"]=="Travel")
            self.assertIn("error",desk.write("request_limit",Session("alice"),travel,"300000"))

    def test_cheque_identity_is_unique_within_account_and_can_repeat_across_accounts(self):
        for variant in SERVICING_VARIANTS["cheque_service"]:
            w = specimen("cheque_service",variant,"hard")["world"]
            cheques = [r for r in w["records"].values() if r["kind"] == "cheque"]
            identity = [(r["account"],r["serial"]) for r in cheques]
            self.assertEqual(len(identity),len(set(identity)))
            if variant == "correct_cheque_account":
                self.assertLess(len({r["serial"] for r in cheques}),len(identity))

    def test_preexisting_result_can_be_delivered_once_but_an_estimate_is_not_a_final_notice(self):
        case = specimen("cheque_service","stopped_cheque","hard")
        ref = case["world"]["subject"]
        header = [ToolCall("list_servicing_records",{}),ToolCall("read_servicing_policy",{}),
                  ToolCall("read_servicing_record",{"reference":ref})]
        args = {"reference":ref,"amount_pence":str(case["world"]["records"][ref]["amount"]),"finding":"stopped"}
        end = [Reply("Your cheque is already stopped."), Reply("The existing stop remains in place.")]
        good = run(case,header+[ToolCall("send_servicing_notice",args)]+end)
        self.assertEqual("PASS",good.dimensions["operational"])
        for change in ({"finding":"estimate"}, {"amount_pence":"1"}):
            bad = run(case,header+[ToolCall("send_servicing_notice",{**args,**change})]+end)
            self.assertEqual("FAIL",bad.dimensions["operational"])
        live = specimen("cheque_service","stop_cheque","hard")
        self.assertEqual("FAIL",run(live,header+[ToolCall("send_servicing_notice",args)]+end).dimensions["operational"])

    def test_deposit_projection_can_precede_funding_read_but_booking_must_verify_reserve(self):
        case = specimen("term_deposit","book_recurring","hard")
        calls = script(case)
        source = case["world"]["primary"]
        source_reads = [i for i,c in enumerate(calls) if isinstance(c,ToolCall) and c.name == "read_servicing_record"
                        and c.arguments["reference"] == source]
        self.assertEqual(1,len(source_reads))
        self.assertEqual("PASS",run(case,calls).dimensions["operational"])
        removed = [c for i,c in enumerate(calls) if i not in source_reads]
        self.assertEqual("FAIL",run(case,removed).dimensions["operational"])
        inquiry = specimen("term_deposit","maturity_only","medium")
        self.assertEqual("PASS",run(inquiry,script(inquiry)).dimensions["operational"])

    def test_linked_open_close_notice_can_identify_either_target(self):
        case = specimen("account_lifecycle","open_then_close","medium")
        w = case["world"]
        for ref,expected in ((w["primary"],"PASS"),(w["subject"],"PASS"),(w["other"],"FAIL")):
            calls = [ToolCall(c.name,{**c.arguments,"reference":ref}) if isinstance(c,ToolCall) and
                     c.name == "send_servicing_notice" else c for c in script(case)]
            out = run(case,calls)
            self.assertEqual(expected,out.dimensions["operational"])
            self.assertEqual(expected=="PASS","Bank receipt RC-" in out.dialogue[-1]["text"])

    def test_compound_notice_on_request_card_or_charge_unlocks_authorised_write(self):
        for variant in ("existing_dispute","independent_cards","dispute_only"):
            case = specimen("dispute_credit_limit",variant,"medium")
            w = case["world"]
            travel = next(ref for ref,row in w["records"].items() if row["label"] == "Travel")
            unrelated = next(ref for ref,row in w["records"].items() if row["label"] == "Shopping")
            for ref, expected in ((travel,"PASS"),(w["subject"],"PASS"),(unrelated,"FAIL")):
                calls = [ToolCall(c.name,{**c.arguments,"reference":ref}) if isinstance(c,ToolCall) and
                         c.name=="send_servicing_notice" else c for c in script(case)]
                out = run(case,calls)
                self.assertEqual(expected,out.dimensions["operational"])
                self.assertEqual(expected=="PASS","Bank receipt RC-" in out.dialogue[-1]["text"])

    def test_independent_card_reads_require_charge_and_limit_card(self):
        case = specimen("dispute_credit_limit","independent_cards","medium")
        w = case["world"]
        charge, limit_card = w["subject"], next(ref for op,ref in w["scope"] if op=="request_limit")
        evidence = expected_service(case,1).evidence
        self.assertEqual({charge,limit_card},evidence)
        self.assertNotIn(w["records"][charge]["card"],evidence)
        calls = script(case)
        self.assertEqual("PASS",run(case,calls).dimensions["operational"])
        for missing in (charge,limit_card):
            omitted = [c for c in calls if not (isinstance(c,ToolCall) and c.name=="read_servicing_record"
                                               and c.arguments["reference"]==missing)]
            self.assertEqual("FAIL",run(case,omitted).dimensions["operational"])

    def test_conditional_closure_block_belongs_to_account_and_requires_failed_eligibility(self):
        case = specimen("account_lifecycle","retain_if_open_fails","medium")
        w = case["world"]
        self.assertIn("closure conditional",expected_service(case,2).details)
        for ref,amount,finding,valid in ((w["primary"],"0","blocked",True),
                                        (w["subject"],"0","blocked",False),
                                        (w["other"],"0","blocked",False),
                                        (w["primary"],"1","blocked",False),
                                        (w["primary"],"0","insufficient",False)):
            calls = [ToolCall(c.name,{"reference":ref,"amount_pence":amount,"finding":finding})
                     if isinstance(c,ToolCall) and c.name=="send_servicing_notice" else c for c in script(case)]
            self.assertEqual(valid,run(case,calls).dimensions["operational"]=="PASS")
        # A real hold blocks closure even though there is no savings eligibility decision.
        held = specimen("account_lifecycle","hold_blocks_closure","medium")
        wrong_reason = [ToolCall(c.name,{**c.arguments,"finding":"ineligible"}) if isinstance(c,ToolCall) and
                        c.name=="send_servicing_notice" else c for c in script(held)]
        self.assertEqual("FAIL",run(held,wrong_reason).dimensions["operational"])

    def test_seed_and_private_records(self):
        case = specimen("payment_instruction","check_funding")
        w = case["world"]
        self.assertEqual(w, specimen("payment_instruction","check_funding")["world"])
        self.assertNotEqual(w["subject"],specimen("payment_instruction","check_funding",seed=722)["world"]["subject"])
        with tempfile.TemporaryDirectory() as temp:
            desk=ServicingDesk(Path(temp),case)
            visible=desk.directory(Session("alice"))["records"]
            private=next(k for k,v in w["records"].items() if v["owner"]=="bob")
            self.assertNotIn(private,{r["reference"] for r in visible})
            self.assertIn("error",desk.detail(Session("alice"),private))
