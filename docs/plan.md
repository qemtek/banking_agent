# Customer-service agent harness

The harness manages conversations, model calls, context limits, tool dispatch and audits. The bank supplies its own tools, middleware, reviewer and policy. The evaluator creates a fresh environment for each trial and grades what happened there. Start the synthetic customer chat with `python3 -m bank_demo --chat` from the project root using a signed-in Codex CLI. The live bank and simulated customer use Terra at medium reasoning effort.

| Guide | Use it to |
| --- | --- |
| [Harness and middleware](guides/middleware.md) | Follow a conversation, change state or context, review a proposed tool call and recover an interrupted write. |
| [Context and compaction](guides/context.md) | Understand input limits, archived tool results, summary generation and adapter guarantees. |
| [Bank scenarios](guides/task-taxonomy.md) | Design cases, generate matched worlds, and combine product Q&A with upsell or eligibility. The bank loads its [escalation policy](policies/escalation.md) into the agent prompt. |
| [Running and grading evaluations](guides/evaluation.md) | Freeze cases, run isolated trials with BenchFlow, review communication and compare two iterations. |
| [Terra medium performance report](terra-medium-report.md) | See the baseline across all 29 families and the proposed paired cheque-servicing comparison with a second bank model. |
| [Terra weakness tracker](guides/model-weaknesses.md) | Find observed failure modes, the runs that exposed them and the evidence needed to reassess them. |
| [Performance and MLflow](guides/performance-measurement.md) | Read success, policy, efficiency, latency, token usage and estimated USD. Open the local dashboard or trace a single turn. |

## Current position

The [task catalogue](guides/task-taxonomy.md) covers 29 families and 118 development outcomes across five customer behaviours and three difficulty levels. The original servicing expansion contributed 59 outcomes across eleven families. Missing-card contact and continuation added six outcomes in the paired pilot. Two additional first-contact outcomes and four provisional-credit outcomes derive from two published Ombudsman decisions. An earlier 100-case run of the original suite produced 92 operational passes. That result predates the expansion. Overall task success still requires review of the customer-facing communication.

The servicing development pilot ran Terra medium on 330 paired worlds. On scorer v7 the standard bank passed 291 operationally and the task-completion skill passed 292. Five families had enough observed headroom for a 10-point gain. The private holdout contains 988 fresh worlds across those families. No confirmatory runs have been made. [Results, uncertainty and cost by family](guides/evaluation.md#november-2026-servicing-results)

In the earlier paired card-selection pilot, Terra passed 28/30 worlds operationally without the task-completion skill and 29/30 with it. One outcome improved, none regressed, and the two-sided paired p-value was 1.0. Nine of 100 turns used more than four tool requests. The busiest used six, well below the 128-call runaway limit. Estimated model cost was $2.09. The bank and customer subsequently received an explicit frozen service date, and the baseline passed both date-related failures in a separate check. Those two observations are outside the paired comparison. [Task-completion skill](../bank_demo/skills/task-completion/SKILL.md) · [Comparison method](guides/evaluation.md#comparing-systems)

The released MVP baseline uses scorer v9. In the matched fee-capped comparison, the standard bank passed all 16 development cases operationally. The [timeout comparisons](guides/evaluation.md#recovering-a-timed-out-replacement) and [large-history retention comparison](guides/evaluation.md#preserving-a-large-case-history) are complete. Both full and compacted histories produced six correct resolutions; ordinary evaluations inherit the adapter’s normal allowance. [Clarification and fee results](guides/evaluation.md#when-clarification-needs-policy)

## Benchmark expansion

### Benchmark-informed servicing tasks

**Implemented.** The two designs below map published banking tasks to executable synthetic scenarios. Each family has easy, medium and hard worlds, per-turn state grading and controls for incorrect actions. The paired pilot compares the standard bank with the task-completion skill. Both the bank and the simulated customer use Terra medium. Research and implementation delegation requires GPT-6.2-Sol at high reasoning or stronger.

| Workstream | Deliverable |
| --- | --- |
| Payments, accounts and credit | State transitions and graders for scheduled payments and standing instructions, cheques, opening and closing accounts, deposit servicing, loan instalments and early repayment, ATM fees, and disputes combined with credit-limit requests. [Servicing designs](guides/servicing-task-design.md) |
| Card operations and fraud | State transitions and graders for tracking a delivery, updating an address, issuing a replacement and deciding how to handle a request after identity or fraud concerns. Pair legitimate and unsafe cases with similar openings. [Card and fraud designs](guides/card-servicing-task-design.md) |
| Environment and measurement | Maintain the isolated SQLite world, frozen service date, audit trail and independent reviewer. Add only the state, tools and simulated actors each workflow needs. Validate the grader and measure independent worlds per family before making an uplift claim. |

#### What to take from the benchmark environments

| Source | Design lesson for our environment |
| --- | --- |
| Sierra τ-Banking | The agent retrieves procedures from a 698-document knowledge base. Two individually reasonable actions can conflict when executed in the wrong order. Our servicing cases exercise these dependencies and offer effective and archived procedure editions against a frozen service date. Each case exposes a small set of procedures. Retrieval across a large corpus remains a separate capability. [Sierra benchmark](https://taubench.com/blog/tau-knowledge.html) |
| TauIndianBankBench | A fresh bank, fixed clock and executable procedures make it possible to check consequential reads, authorized writes and extra mutations. Capture quote delivery, confirmation and commitment as distinct events. Accept different safe routes to the same outcome. [Environment and scoring](https://arxiv.org/html/2610.30161v1) |
| NPCI IndicBankBench | Multi-turn cases, mocked tools and capability/refusal scenarios exercise what an assistant can do when inputs, tools or permissions are insufficient. Add fixtures with incomplete information and unavailable services alongside routine successes. [Repository and evaluation design](https://github.com/npci/IndicBankBench) |
| FraudBench | Both actors can affect shared state. An earlier event can change which dispositions are safe later. Start with narrow customer-side actions such as responding to a verification request through the simulated app. Keep trusted results out of customer-authored text. [Paper and environment](https://arxiv.org/abs/2608.18136) |
| Nubank | Its published production study uses simulated customers for delivery and card management, then validates changes against live outcomes. Model tracking and reissue as asynchronous processes with address and dispatch cutoffs. Our synthetic suite can measure whether the simulated operation succeeds. Claims about customer satisfaction require separate evidence. [Production evaluation](https://arxiv.org/abs/2609.30137) |

#### Completed stages

1. Documented each workflow’s customer objective, decisive facts, permitted actions and expected state. The family guides distinguish implemented rules from future extensions.
2. Built the generators and isolated bank environments. Reference runs covered all 59 new outcomes at all three levels. Grading uses per-turn snapshots, action history and delivered receipts. Communication that requires interpretation goes to review.
3. Ran both arms on the same 30 development worlds per family. A separate loan workflow checked what happened when a signed settlement instruction was released or withdrawn after the quote changed. [Loan workflow](guides/servicing-task-design.md#exploratory-check-on-a-new-loan-workflow)
4. Sized independent held-out samples for a 10-percentage-point gain with 80% power at a two-sided 5% significance level. The five sized families have frozen local casebooks. The published results include assumptions, costs, uncertainty and the effect of grader corrections. [Family results](guides/evaluation.md#november-2026-servicing-results) · [Power estimator](../evaluation/power.py)

<a id="next-build--missing-card-after-an-address-change"></a>

## Missing card after an address change

The two tasks ask whether the bank can resolve a missing card when the customer has moved, the previous adviser's request timed out and the carrier has no scan. One starts at first contact. The other starts a fresh agent from a frozen conversation and an investigation due for an update. Both use the same bank policy and tool menu. [Decision table and worked cases](guides/card-servicing-task-design.md#missing-card-after-a-move)

| Work | Evidence of completion |
| --- | --- |
| Establish which facts authorize a decision | The current issuer response and certified archive can establish dispatch or final failure. The profile, courier and signed app instruction answer separate questions. A named owner, next action and deadline are required while both issuer routes are inconclusive. |
| Generate direct and compound siblings | Siblings share the opening objective, physical order state, signed events and promised update. Compound cases add competing records, provisional responses or a stale adviser note. A valid signature can name a destination different from both the profile and the prior shipment. |
| Grade the contact and continuation independently | Each turn records the evidence seen before the action, attempted writes, actual bank state and public reply. The continuation must resolve and close the existing case or retain it with the required owner. Communication requiring interpretation goes to review. |
| Check the decision boundary | In both families, a hidden commit and a hidden noncommit produce identical public evidence and pass with the same investigation. Changing only the public archive result to confirm dispatch makes blanket deferral fail. Offline controls also cover retries, withdrawn authority, an unsupported lucky guess and missing follow-up ownership. |
| Calibrate difficulty | Completed 48 BenchFlow conversations with Terra medium for both roles. Original contact passed 22/24 operationally and continuation passed 21/24. Five cases stalled after a signed instruction arrived. Compound siblings used 15 extra tool requests in total. The paired intervals are too wide to conclude they reduced success. [Pilot results and uncertainty](guides/evaluation.md#missing-card-development-pilot) |

### Public-case expansion

Reviewed the 29 families against published Ombudsman cases and benchmark designs. The first implemented pair starts with a declined purchase and requires the bank to trace it to a previous misdelivery. The source decision and both counterfactuals belong to development. An independent decision is reserved by ID. The refund family now follows a closed dispute through reinstatement and the merchant's subsequent settlement. Four matched outcomes cover full, partial, pending and unrelated credits. The next identified gap is tracing a standing order across both accounts after a manual payment. [Evidence and gaps by family](guides/task-taxonomy.md#public-case-coverage)

### Choosing the next difficult cases

Start with the missing-card journey because the same opening can legitimately lead to a trace, a replacement or an investigation. Its matched pilot can show whether reconciling evidence changes how often the bank reaches the right outcome.

| Candidate after the card pilot | Decision that makes it difficult | Matched change to the evidence |
| --- | --- | --- |
| Cancel a scheduled payment after a timeout | Determine whether a cancellation took effect before cutoff and whether another attempt is safe. | Keep the customer's request and payment fixed. Vary whether a definitive cancellation receipt is available, execution is confirmed or the final state is still unknown. |
| Settle a loan after the quote changes | Establish which amount the customer authorized and whether settlement already occurred. | Start from the same accepted quote. Change one fact at a time, such as a new effective quote, a fresh signed instruction or a withdrawn instruction. Extend the existing exploratory loan workflow. |

Review the card pilot before choosing either candidate. If Terra struggles, identify the missing evidence, incorrect inference or unsafe action from the trace. If a simulator leak or grader error explains the result, correct the measurement first. If Terra solves both siblings consistently, record that result and choose a different decision to study. Each expansion should introduce an identifiable decision, have at least one resolvable case and one justified deferral, and have an observable consequence that can be scored independently. More turns or more records alone do not establish difficulty.

### Other evaluation work

1. Complete blind communication review before reporting overall task success.
2. Choose a single family and skill for confirmation. Term deposits have the lowest projected cost at $10.41 for 111 pairs. Choose another family if its observed failures better match what the skill addresses. Check that the planning disagreement remains credible for any new candidate.
3. Broader retention claims need independent histories and workflows beyond the two synthetic accounts in the completed large-history comparison.

The demo uses synthetic services, host-authenticated sessions and a local audit store. Production deployment would also require retention and access controls, distributed checkpoint ownership and idempotent writes to external services.
