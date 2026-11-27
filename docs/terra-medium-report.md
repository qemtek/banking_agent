# How Terra medium performs on the banking tasks

**27 November 2026.** On the largest cohort, the standard bank running `gpt-6.1-terra` at medium effort passed **293 of 330 servicing conversations operationally (88.8%)**. It passed every account-lifecycle, ATM-fee, card-delivery and combined dispute/credit-limit case. Cheque servicing (19/30) and loan servicing (21/30) were its weakest families in that cohort. On loans, all nine failures gave the customer the right figure. The failure was giving it before establishing which terms applied. [Servicing evidence](guides/evaluation.md#reading-loan-terms-before-sending-a-figure)

**How often did the entire interaction succeed?** Five servicing conversations passed every criterion, 39 had a known failure and 286 still need a communication judgement. An operational pass verifies the required bank actions, records and procedures. It can coexist with wrong advice. In one identity-review workflow the bank filed the required referral and delivered the receipt, then told an already-signed customer to sign again. A reliable overall customer-service success rate remains unknown. [Grading dimensions](guides/evaluation.md#reading-scores) · [Identity-review evidence](guides/evaluation.md#grading-an-identity-review-referral)

## Results across the catalogue

The table covers all **29 generated task families** across **547 archived development attempts**. Each cohort used one attempt per case and an LLM customer, with some frozen return messages. Model settings were the same across cohorts, while tool interfaces, limits, prompt revisions and case sampling changed. Read each row against its own denominator. The whole-conversation columns include every attempt. All six underlying batches completed without runner errors.

| Cohort | Operational passes | Whole pass | Whole fail | Whole review |
| --- | ---: | ---: | ---: | ---: |
| Core customer service | 92/100 | 0 | 12 | 88 |
| Product advice and applications, early interface | 15/39 | 0 | 24 | 15 |
| Card selection, revised interface and 128-call guard | 28/30 | 0 | 2 | 28 |
| Servicing, 11 families | 293/330 | 5 | 39 | 286 |
| Missing-card contact and continuation | 43/48 | 0 | 14 | 34 |

| Cohort | Operational passes by family |
| --- | --- |
| Core | Routing 27/27, follow-up 13/13, evidence 11/12, approval 9/9, handoff 8/12, refund 7/9, receipt 5/6. Access, availability, explanation and lookup each passed 3/3. |
| Early products | Product Q&A 6/6, card application 9/12, eligibility 0/9, upsell 0/6, card selection 0/6. |
| Revised card selection | Card selection 28/30. The casebook and interface also changed, so the difference from 0/6 cannot be attributed to the tool limit alone. |
| Servicing | Account lifecycle, ATM fee, card delivery and combined dispute/credit limit each passed 30/30. Card address change and card identity each passed 29/30, card reissue and payment instruction each passed 26/30, term deposit 23/30, loan service 21/30 and cheque service 19/30. |
| Missing card | Original contact 22/24 and continuation from a previous adviser 21/24. Each family had 12 base worlds with two versions apiece. |

The servicing cohort sampled 30 independent worlds per family. Easy cases passed 98/110 (89.1%), medium 95/109 (87.2%) and hard 100/111 (90.1%). These labels describe scenario construction and showed no increasing failure rate here. Even 30/30 has a two-sided 95% Wilson lower bound of 88.6%. The mix of synthetic cases does not establish how frequently these outcomes occur at a real bank. [Task taxonomy](guides/task-taxonomy.md) · [Sampling design](guides/evaluation.md#november-2026-servicing-results)

## What the failures tell us

| Finding | Evidence and interpretation |
| --- | --- |
| Correct action and correct advice can diverge | Across eight identity-review worlds run under two prompts, Terra filed 15/16 required referrals and delivered all 15 receipts. Ten whole conversations passed and six failed. Five failures gave stale signing advice across three distinct orders. One explicitly requested referral was never filed. The four formerly unresolved conversations passed after the grader verified their opening order lookup, invitation and agreement to wait. These conversations overlap the servicing pilot. [Referral audit](guides/evaluation.md#grading-an-identity-review-referral) |
| Terra can reconcile changing records and preserve scope | On three hard reopened-dispute worlds, each with four merchant outcomes, it reconciled temporary relief and later credits correctly in 12/12. In a separate fee-cap study it handled 16/16 cases, including all eight signed instructions whose fee exceeded the customer’s cap. These are controlled variations on a few situations. [Credit reconciliation](guides/evaluation.md#provisional-credit-development-pilot) · [Fee-cap study](guides/evaluation.md#when-clarification-needs-policy) |
| It can use a figure before checking whether it applies | In the nine loan failures, Terra omitted the effective-policy lookup six times and performed it too late three times. Eight of the eleven cheque failures also failed an evidence requirement. On 12 fresh loan worlds, an experimental instruction to read the terms before the first customer notice improved timely reads from 4/12 to 12/12. The standard-bank result above uses the original prompt. [Loan traces and intervention](guides/evaluation.md#reading-loan-terms-before-sending-a-figure) |
| It can miss a change between contacts | In the original missing-card cohort, five of eight newly signed returns ended with no recheck and no card. In a later paired study, requiring a fresh lookup raised operational passes from 11/18 to 18/18. Combining the lookup rule with a clarification about which reference to share improved receipt delivery from 0/8 to 8/8 on signed returns, while all 16 unsigned cases stayed unissued. Those later bank rules were adopted. Their advantage on new task families remains unmeasured. [Return lookup](guides/evaluation.md#refreshing-the-app-decision-on-return) · [Receipt delivery](guides/evaluation.md#combining-the-app-lookup-and-receipt-rules) |
| Some apparent weaknesses belong to the harness or task interface | In the early product cohort, 21 personal-advice cases ended without the requested portal decision. In three application cases Terra requested the correct tool and the four-call guard blocked it. On the external Sierra τ-Banking pilot, all four attempts on two card-selection tasks transferred before an application was filed. These are useful failure examples, but the tools, simulator and grader differ from the internal 28/30 card-selection run. [Product cohort](guides/task-taxonomy.md#generating-card-cases-and-composing-conversations) · [External pilot](guides/evaluation.md#matched-refund-and-external-banking-pilots) |

**Efficiency on the 330 servicing conversations.** Terra used a median of **7 bank tool requests per conversation**, with a 95th percentile of **12** and a maximum of **14**. Across 665 bank turns, median response time was **42.2 seconds** and p95 was **73.8 seconds**, measured from bank turn start to finish. Estimated usage was **$13.40 for the bank** (4.1¢ per attempt) and **$1.38 for the customer**, or **$14.79 combined** before rounding the components. These are archived token-rate equivalents for subscription runs. One attempt per world measures variation between cases. Measuring repeatability requires repeated attempts on the same worlds. [Measurement definitions](guides/performance-measurement.md)

## Comparing the next bank model

1. **Start with the 30 development cheque worlds.** They span easy, medium and hard cases and leave room above Terra’s historical 19/30. The 30 case IDs and app events are frozen in the local casebook listed below. Run both banks afresh. Use the same scripted customer for both arms to isolate the bank change. This creates a new Terra control alongside the challenger. The historical runs used an LLM customer.
2. **Freeze the comparison before execution.** Record the casebook digest, code and policy hashes, scorer, tool schemas, service date, model IDs, provider, reasoning settings and effective context/output limits. Keep the same bank instructions, tools, runaway guard and concurrency. The runner currently shares a model factory across live actors, and the paired comparison command requires equal model settings. The next implementation is a bank-only OpenRouter adapter and an explicit cross-model comparison mode that preserves those other checks. [Runner](../bank_demo/casebook.py) · [Pairing checks](../evaluation/uplift.py)
3. **Use operational pass on the first attempt as the primary endpoint.** Publish paired wins, losses and ties, the percentage-point difference and a paired 95% interval. Show whole-conversation pass/fail/review, policy violations, bank tool calls, latency, tokens and bank-only USD alongside it. Keep runner errors visible for each arm and disclose any incomplete pairs. Compare provider-reported spend and rate-card equivalents under separate labels.
4. **Use the pilot to plan a fresh confirmation.** The 30 development pairs can reveal *which* errors change. For a claim about a 10-percentage-point difference, use the new pair’s disagreement rate to check the required sample before opening a holdout. The earlier allocation of 220 cheque worlds used disagreement between two Terra prompts. Reserve the independent worlds until the endpoint, sample size and model settings are fixed. [Power assumptions](guides/evaluation.md#november-2026-servicing-results)

## Reproducing the figures

From the project root, run the following against the locally retained development archives. The first command verifies complete casebooks and replays each archived conversation using scorer v14. Its JSON includes the six batch IDs, casebook hashes, replay source hash, verdicts and timing distributions. The second reproduces the separate identity-review analysis. The original 100-case archive predates per-turn SQLite snapshots and uses the recorded state evidence where a snapshot is unavailable.

```sh
python -m bank_demo.terra_baseline > audits/terra-medium-report-v14.json
python -m bank_demo.referral_validation --output audits/identity-review-grader-v14.json
```

The prepared comparison casebook is `audits/casebooks/model-comparison-cheques-development.json`. Its canonical SHA-256 is `6e4d8376d92a8136dfe48de06582ec73256faabab319214283a5657ffccd01ec`. To reconstruct it from the retained development batch, keep the original metadata and case order.

```sh
jq '.cases |= map(select(.family == "cheque_service"))' \
  audits/evaluations/5dd2495a11d54ecab5dadd92c30f631f/casebook.json \
  > audits/casebooks/model-comparison-cheques-development.json
```

The earlier v7 servicing report gives 291/330 on the same bank replies. The later clarification rubric accepted the evidence available on one payment and one cheque enquiry, yielding 293/330. This is a scoring revision. All figures here come from saved runs. No challenger or private holdout was run for this report. [Earlier result](guides/evaluation.md#november-2026-servicing-results) · [Clarification rubric](guides/evaluation.md#when-clarification-needs-policy)
