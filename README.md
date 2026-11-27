# Customer-service agent harness

A Python learning project for building and evaluating a tool-using customer-service agent. The harness manages conversations, tool calls, approvals, context compaction and local audit logs. The example bank supplies its own policies, middleware and synthetic accounts. No real bank connection is required.

The bank generates 118 development outcomes across 29 task families. Missing-card cases cover a first enquiry, a promised update and a declined purchase that leads the bank to an earlier misdelivery. Refund cases distinguish temporary bank relief from a merchant refund for the disputed purchase. The [source map](docs/guides/task-taxonomy.md#public-case-coverage) records the public cases behind the new work. [Missing-card development pilot](docs/guides/evaluation.md#missing-card-development-pilot)

Terra medium passed 293 of 330 servicing cases operationally under the current grader. Five whole conversations passed, 39 had a known failure and 286 await a communication judgement. The baseline report covers all 29 families, measured costs, strengths and weaknesses, and the design for a paired model comparison. [Terra medium performance report](docs/terra-medium-report.md)

The public baseline is tagged `mvp-eval-baseline-v1`. Later [release comparisons](docs/guides/evaluation.md#reproducing-the-release-comparisons) cover recovery after a card-order timeout and retention of decisive facts when a long dispute history is compacted.

The [Terra weakness tracker](docs/guides/model-weaknesses.md) records recurring failures, examples from conversations and results of follow-up experiments.

## Run a conversation

Use Python 3.12 on macOS or Linux with a signed-in Codex CLI and access to `gpt-6.1-terra`. The demo uses medium reasoning effort. From the project root, run:

```sh
python3.12 -m bank_demo --chat
```

Try asking for your available balance, then ask about recent transactions in the same session. Type `/quit` to finish. The demo writes a local audit under `audits/`.

## Run an evaluation

With Docker running and `uv` installed, generate two synthetic cases and run the bank and customer in a simulated conversation:

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv -r requirements-benchflow.txt -r requirements-tracking.txt
source .venv/bin/activate
python -m bank_demo.casebook generate audits/casebooks/example.json --seed 41 --family card_selection --difficulty easy --behaviour cooperative
python -m bank_demo.casebook run audits/casebooks/example.json --arm baseline --customer-mode llm
```

Each case gets an isolated bank environment. Its scorecard records the final bank state, actions and customer conversation. A `REVIEW` verdict means a required judgement is pending. To inspect the runs and traces, run `python -m evaluation.tracking import`, then `python -m evaluation.tracking ui` and open `http://127.0.0.1:5055`. Run artifacts stay on your machine.

See the [project status and guide index](docs/plan.md), [middleware flow diagrams](docs/guides/middleware.md) and [bank task catalogue](docs/guides/task-taxonomy.md).
