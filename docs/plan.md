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

The generator covers 16 families and 46 outcomes across five customer behaviours and three difficulty levels. In an earlier 100-case development run, Terra passed 92 cases operationally. Overall task success still requires review of the customer-facing communication.

In the latest paired card-selection pilot, Terra passed 28/30 worlds operationally without the task-completion skill and 29/30 with it. One outcome improved, none regressed, and the two-sided paired p-value was 1.0. Nine of 100 turns used more than four tool requests. The busiest used six, well below the 128-call runaway limit. Estimated model cost was $2.09. The bank and customer subsequently received an explicit frozen service date, and the baseline passed both date-related failures in a separate check. Those two observations are outside the paired comparison. [Task-completion skill](../bank_demo/skills/task-completion/SKILL.md) · [Comparison method](guides/evaluation.md#comparing-systems)

## Next decisions

1. Complete blind communication review before reporting overall task success.
2. For a future skill comparison, choose a family with enough headroom for the target improvement, then size a fresh held-out sample from paired pilot outcomes.
3. Evaluate compaction on long conversations where an earlier decision or correction changes the final bank state. Compare correctness, policy compliance, cost and latency across context settings.

The demo uses synthetic services, host-authenticated sessions and a local audit store. Production deployment would also require retention and access controls, distributed checkpoint ownership and idempotent writes to external services.
