import argparse
import json
from pathlib import Path
from uuid import uuid4

from evaluation.tracking import publish_if_configured
from evaluation.usage import estimate_cost, price_card, usage_scorecard

from evaluation import evaluate
from harness import DEFAULT_MAX_TOOL_CALLS
from evaluation.scoring import SCORE_VERSION, exit_code
from adapters.codex_cli import model_factory
from . import BANK_PROMPT_VERSION, POLICY_SHA256
from .customer import ClaimCustomer, PERSONAS
from .tasks import bank_tasks
from .ledger_tasks import claim_tasks, ledger_tasks


def main():
    parser = argparse.ArgumentParser(description="Evaluate the bank agent on synthetic customer scenarios.")
    parser.add_argument("--task")
    parser.add_argument("--suite", choices=("all", "baseline", "ledger", "claims"), default="all")
    parser.add_argument("--customer-mode", choices=("scripted", "llm", "both"), default="scripted",
                        help="Customer for the two-turn refund claims")
    parser.add_argument("--customer-persona", choices=tuple(PERSONAS), help="Brief for the LLM customer (default: mistaken)")
    parser.add_argument("--ledger-environment", choices=("original", "busy"), default="busy",
                        help="Bank data and tool catalogue for SQLite scenarios")
    parser.add_argument("--seed", type=int, default=41, help="Seed for synthetic SQLite statements")
    parser.add_argument("--require-verified-context", action="store_true",
                        help="Require provider token verification and an enforced generation cap")
    parser.add_argument("--output", type=Path, default=Path("audits/evaluations"))
    args = parser.parse_args()
    try:
        create_model = model_factory(require_verified_context=args.require_verified_context)
    except ValueError as error:
        parser.error(str(error))
    tasks = []
    if args.suite in ("all", "baseline"):
        tasks.extend(bank_tasks(create_model))
    if args.suite in ("all", "ledger"):
        tasks.extend(ledger_tasks(create_model, args.seed, args.ledger_environment))
    if args.suite in ("all", "claims"):
        tasks.extend(claim_tasks(create_model, args.seed, args.ledger_environment))
    if args.task and args.task not in {task.name for task in tasks}:
        parser.error(f"Unknown task: {args.task}")
    tasks = [task for task in tasks if not args.task or task.name == args.task]
    if args.customer_mode != "scripted" and not any(t.name.startswith("claim_") for t in tasks):
        parser.error("The LLM customer requires a refund claims task")
    if args.customer_persona and args.customer_mode == "scripted":
        parser.error("A customer persona requires --customer-mode llm or both")
    persona = args.customer_persona or "mistaken"
    directory = args.output / uuid4().hex
    directory.mkdir(parents=True)
    model = create_model()
    manifest = {"suite": args.suite, "seed": args.seed, "score_version": SCORE_VERSION, "fixture_version": 2,
                "ledger_environment": args.ledger_environment,
                "bank_prompt_version": BANK_PROMPT_VERSION, "policy_sha256": POLICY_SHA256, "model": model.model_id, "reasoning_effort": model.reasoning_effort,
                "max_tool_calls": DEFAULT_MAX_TOOL_CALLS, "model_timeout_seconds": model.timeout_seconds,
                "window": model.context_budget.window, "reserve": model.context_budget.reserve,
                "unit": model.context_budget.unit, "tasks": [t.name for t in tasks],
                "customer_mode": args.customer_mode, "price_card": price_card()}
    if args.customer_mode != "scripted":
        manifest["customer"] = {"persona": persona, "brief_version": 1, "model": model.model_id,
                                "reasoning_effort": model.reasoning_effort}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    outcomes = []
    for task in tasks:
        modes = ("scripted",)
        if task.name.startswith("claim_"):
            modes = ("scripted", "llm") if args.customer_mode == "both" else (args.customer_mode,)
        for mode in modes:
            customer = ClaimCustomer(create_model(), persona, directory / "customers") if mode == "llm" else None
            outcome = evaluate(task, directory, customer=customer)
            row = {**usage_scorecard(outcome, customer), "customer_mode": mode}
            row["estimated_cost_usd"] = estimate_cost(row, manifest)
            outcomes.append(row)
            cost = row["estimated_cost_usd"].get("total")
            spend = f"${cost:.6f}" if cost is not None else "unknown"
            print(f"{outcome.status}  {task.name}  customer={mode}  estimated cost {spend}  {outcome.audit}")
            for check in outcome.checks:
                label = "REVIEW" if check.passed is None else "PASS" if check.passed else "FAIL"
                print(f"  {label}  [{check.category}] {check.criterion} — {check.evidence}")
            if outcome.error:
                print(f"  {outcome.error}")

    report = directory / "report.json"
    report.write_text(json.dumps(outcomes, indent=2) + "\n", encoding="utf-8")
    print(f"Report  {report}")
    publish_if_configured(report)
    return exit_code(outcomes)


if __name__ == "__main__":
    raise SystemExit(main())
