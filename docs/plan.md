# Customer-service agent harness

The harness manages conversations, model calls, context limits, tool dispatch and audits. The bank supplies its own tools, middleware, reviewer and policy. The evaluator creates a fresh environment for each trial and grades what happened there. Start the synthetic customer chat with `python3 -m bank_demo --chat` from the project root using a signed-in Codex CLI. The live bank and simulated customer use Terra at medium reasoning effort.

| Guide | Use it to |
| --- | --- |
| [Harness and middleware](guides/middleware.md) | Follow a conversation, change state or context, review a proposed tool call and recover an interrupted write. |
| [Context and compaction](guides/context.md) | Understand input limits, archived tool results, summary generation and adapter guarantees. |
| [Bank scenarios](guides/task-taxonomy.md) | Design cases, generate matched worlds, and combine product Q&A with upsell or eligibility. The bank loads its [escalation policy](policies/escalation.md) into the agent prompt. |
| [Running and grading evaluations](guides/evaluation.md) | Freeze cases, run isolated trials with BenchFlow, review communication and compare two iterations. |
| [Performance and MLflow](guides/performance-measurement.md) | Read success, policy, efficiency, latency, token usage and estimated USD. Open the local dashboard or trace a single turn. |

## Current position

The [task catalogue](guides/task-taxonomy.md) covers 27 families and 105 outcomes across five customer behaviours and three difficulty levels. Eleven servicing families add 59 outcomes. An earlier 100-case run of the original suite produced 92 operational passes. That result predates the expansion. Overall task success still requires review of the customer-facing communication.

The servicing development pilot ran Terra medium on 330 paired worlds. The standard bank passed 291 operationally and the task-completion skill passed 292. Five families had enough observed headroom for a 10-point gain. The private holdout contains 988 fresh worlds across those families. No confirmatory runs have been made. [Results, uncertainty and cost by family](guides/evaluation.md#november-2026-servicing-results)

In the earlier paired card-selection pilot, Terra passed 28/30 worlds operationally without the task-completion skill and 29/30 with it. One outcome improved, none regressed, and the two-sided paired p-value was 1.0. Nine of 100 turns used more than four tool requests. The busiest used six, well below the 128-call runaway limit. Estimated model cost was $2.09. The bank and customer subsequently received an explicit frozen service date, and the baseline passed both date-related failures in a separate check. Those two observations are outside the paired comparison. [Task-completion skill](../bank_demo/skills/task-completion/SKILL.md) · [Comparison method](guides/evaluation.md#comparing-systems)

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

### Other evaluation work

1. Complete blind communication review before reporting overall task success.
2. Choose a single family and skill for confirmation. Term deposits have the lowest projected cost at $10.41 for 111 pairs. Choose another family if its observed failures better match what the skill addresses. Check that the planning disagreement remains credible for any new candidate.
3. Evaluate compaction on long conversations where an earlier decision or correction changes the final bank state. Compare correctness, policy compliance, cost and latency across context settings.

The demo uses synthetic services, host-authenticated sessions and a local audit store. Production deployment would also require retention and access controls, distributed checkpoint ownership and idempotent writes to external services.
