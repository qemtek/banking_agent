# Measuring agent performance

The unit of evaluation is **one conversation on one frozen case**. For each agent iteration, show what it solved, which rules it broke, and what it spent getting there. Keep the case ID so that a change in task difficulty cannot masquerade as a change in agent performance.

## Scorecard

| Question | What to report | Evidence available today |
| --- | --- | --- |
| Did the agent solve the problem? | Counts of pass, fail, awaiting review and execution error across all attempts. Show operational correctness and customer communication separately. Give the assessed count beside each pass rate. | Version 5 scorecards and MLflow run metrics. Saved bank state, ordered actions and the delivered reply supply different evidence. |
| Did it follow the rules? | For each applicable rule, count cases with a violation over cases assessed for that rule. Distinguish an unauthorized proposal, a blocked attempt and an unauthorized committed write. Track missed required escalations and unnecessary escalations separately. | Approval events, mutation journals, queue records and category checks exist. Some disclosures require review of the delivered text. Per-rule case rates are a proposed rollup. |
| How much work did it do? | Tool **requests** and model calls per conversation, including unsuccessful attempts. Show median and range across repeats of the same case. In paired comparisons, show calls saved on cases both agents solved alongside wins, losses and the number of matched solves. | Audits record requests and dispatches. MLflow shows tool execution spans. New MLflow runs also show the conversation request count, busiest turn and guardrail stops. |
| Does it repeat itself? | Count additional same-turn read requests with identical tool and arguments. Also count conversations where the bank repeats an entire reply or asks for information the customer already supplied. Show an example when investigating a flag. | Selected duplicate reads are already checked. Exact repeated replies can be counted from public dialogue after normalizing case and whitespace. Rephrased questions and whether a repeat was justified need a labelled review sample. |
| How long does the customer wait? | Report median and p95 per-turn time from customer message received to public bank response. For each conversation, sum the bank response intervals. Show model time, tool time and human approval wait separately. Report timeouts and sample sizes alongside percentiles. | Turn, model, tool and approval timestamps exist. The native trace duration can include simulated customer generation. For deferred approval, end the first response interval when the pending notice is delivered. |
| What does a resolution cost? | Show estimated USD per conversation for the bank and simulator separately. Divide bank spend across all attempts by verified resolutions to include work spent on failures. | Evaluations save provider token counts and estimated USD. Historical cases without usage have unknown cost. |

Repeated actions are diagnostic. Refreshing a statement after a credit posts may be necessary. Show the outcome beside the request count. On the 100 development cases, the median was four requests and the range was 0–12. One handoff took seven requests and five on its repeat.

## Reading estimated spend and tokens

Run `python -m evaluation.usage PATH/TO/report.json` to see estimated USD first. In the MLflow runs table, select `estimated_cost_usd` for the whole experiment trial or `bank_estimated_cost_usd` for the service agent alone. The simulator has its own `customer_estimated_cost_usd` metric. These figures express usage at standard Codex Enterprise rates. Plus billing is separate.

Each new batch saves the rate table in its manifest and the resulting dollars in each scorecard. To update prices, copy the latest dated entry in the [local price table](../../evaluation/token_prices.json), give it a new date and update its rates and source. The newest dated entry applies to future batches. Earlier reports that recorded tokens before pricing began use the initial November 25, 2026 card. An unknown model or missing cache breakdown leaves the affected estimate unavailable. The calculation charges uncached input, cached input and output once each. [OpenAI standard Codex rates](https://help.openai.com/en/articles/20001415-chatgpt-rate-card-enterprise-token-based-pricing)

Each actor has its own `calls` and `reported_calls`. On partial coverage, `reported_tokens` is the known subtotal. A full `total_tokens` requires input and output counts for every observed call. The batch ratio requires complete usage, at least one verified resolution and no outstanding task verdict. While communication review is pending, `tokens_per_operational_pass` uses the known operational passes. Pass the reviewed report to update the full-success denominator. Fixed customer messages make zero model calls.

The live balance enquiry used an estimated **$0.008219** across three model calls. The bank accounted for $0.005777 and the simulated customer for $0.002442.

Decision, simulator and history-summary audit events include a `usage` object when the provider returns counts, even if the response subsequently fails validation. Cached input is part of input tokens. Reasoning output, when supplied, is part of output tokens. A timeout or interrupted call can have unknown spend. BenchFlow carries the host's usage into the sandbox audit. The Responses adapter records generation usage independently of its preflight input count.

## Dashboard in MLflow

Start the local UI with `.venv/bin/python -m evaluation.tracking ui`. Initialize the store or backfill existing executions with `.venv/bin/python -m evaluation.tracking import`. Future batches export automatically once the store exists. Import a reviewed report by supplying its path. Its `measurement=regrade` tag distinguishes it from the original execution. Open a run’s **Traces** tab for model and tool spans with the original timestamps.

| View | How to use it |
| --- | --- |
| **Usage and Tool calls** | Open the experiment's Dashboard for turn volume, latency, trace errors and tool execution patterns. Open Traces or Sessions to inspect an outlier. Our exporter creates one trace per turn, and a trace can include the customer model. A tool request stopped before dispatch has no tool execution span. [Local dashboard](http://127.0.0.1:5055/#/experiments/1/dashboard/usage?workflowType=genai) · [MLflow dashboard documentation](https://mlflow.org/docs/latest/genai/tracing/observe-with-traces/dashboard/) |
| **Outcomes by iteration** | Use the experiment runs table and Compare with the logged model, reasoning effort and arm. Select the same casebook, customer mode and scorer version. Use one scorecard version per execution. A regrade links back to the original run and represents the same agent work. Inspect `operational_correct`, `communication_correct`, their `_assessed` metrics and the `eval_status` tag. [Local experiment](http://127.0.0.1:5055/#/experiments/1) · [MLflow Tracking UI](https://mlflow.org/docs/latest/ml/tracking/) |

Open the bank evaluation experiment in your local MLflow UI. In **Model training → Runs**, select two runs to compare their parameters and metrics. For an iteration trend, use one summary point per arm on the same casebook. A chart of individual trials ordered by time also reflects which cases happened to run first.

MLflow LLM spans carry native token attributes. A trace sum can be partial if one call lacks usage. In the runs table, use `bank_total_tokens` and `customer_total_tokens` for attribution, and check each actor’s reported-call count. On historical traces the dashboard can show zero for missing usage. Historical spend is **unknown**. The trace error rate also answers a different question from task failure. For example, a service outage can produce an error trace while the agent handles the outage correctly.

## Comparing models, effort and system iterations

1. Identify each arm by bank model, reasoning effort, agent revision and prompt or policy version. Record tool catalogue, context settings and adapter version when they change. Today the generated manifest records the arm, model, effort, prompt version, policy and instruction hashes. An immutable revision covering all harness code is still needed.
2. Pair on frozen case ID and repeat number. Hold casebook, customer configuration, environment, scorer and review sample fixed. Keep concurrency comparable for latency. For a bank-model comparison, pin the customer model independently or use the scripted customer. The current live runner uses the same model factory for both actors.
3. Show paired success change and its uncertainty first. Give each independent case equal weight. Then show the paired difference in calls, time and tokens on common solves, with coverage. Show spend and execution errors across all attempts as well. For repeatability, display each case's outcomes and range across repeats before pooling different workflows. [Paired comparison method](evaluation.md#comparing-systems)

**Implementation order.** Start with the native dashboard and existing scorecards. Next publish duplicate reads, bank response time and policy-violation counts as MLflow run metrics, with versioned definitions so they can be backfilled. Then publish one summary per matched experiment comparison for iteration trends. The current uplift command compares iterations at fixed model and effort. Future model or effort comparisons should explicitly declare which setting is being varied. The live baseline remains Terra at medium effort.
