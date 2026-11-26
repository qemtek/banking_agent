# Customer-service agent harness

A Python learning project for building and evaluating a tool-using customer-service agent. The harness manages conversations, tool calls, approvals, context compaction and local audit logs. The example bank supplies its own policies, middleware and synthetic accounts. No real bank connection is required.

The bank generates 105 outcomes across 27 task families. In a 330-world paired development pilot, the standard bank passed 291 cases operationally and the same bank with a task-completion skill passed 292. The evaluation guide reports results by family, estimated costs and sample sizes for a future comparison. [Pilot results](docs/guides/evaluation.md#november-2026-servicing-results)

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
