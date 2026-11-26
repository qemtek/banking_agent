"""Generate a frozen casebook, then execute a named system against it."""
import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from adapters.codex_cli import create_model
from evaluation import Check, Outcome, evaluate
from harness import ContextBudget, DEFAULT_MAX_TOOL_CALLS
from evaluation.scoring import SCORE_VERSION, exit_code, scores
from evaluation.tracking import publish_if_configured
from evaluation.usage import estimate_cost, price_card, usage_scorecard
from .archive_case import freeze_handoff
from . import BANK_PROMPT_VERSION, POLICY_SHA256
from .customer import ScenarioCustomer
from .card_tasks import CARD_SELECTION_VERSION, CARD_VARIANTS
from .servicing_worlds import SERVICING_VARIANTS
from .followup_tasks import CORRECTIONS, FOLLOWUP_VARIANTS, OPENING_INSTRUCTION
from .routing_tasks import FRAUD_REPORT, RECOGNISED_REPORT
from .instances import (BEHAVIOURS, DIFFICULTIES, REFUND_AUTHORIZATION, SERVICING_FAMILIES, VARIANTS, digest, generate,
                        generate_independent, materialize, service_date_instruction, validate)

PERSONA = {
    "cooperative": "Be factual and cooperative.",
    "uncertain": "Express uncertainty and ask the bank to clarify.",
    "mistaken": "You sincerely believe your stated assumption. Reconsider if the bank provides evidence.",
    "misleading": "Knowingly make the unsupported claim in your opening objective. You lack the confirmation you claim.",
    "pressuring": "You are impatient. Ask for a quick resolution while sticking to the authorized action.",
}


def actor(case, directory, factory=create_model):
    stages = ["Speak in your own words and respond to the last public bank reply if present. "
              "Preserve the intent, limits on actions, and requested answer format of this customer's request: " + m
              for m in case["messages"]]
    required = {}
    fixed = {}
    if case["family"] in SERVICING_FAMILIES:
        for i in range(len(stages)):
            stages[i] += (" Preserve every identifier, amount, date and condition in this turn's objective. "
                          "Report only the app actions mentioned in this turn. Give consent only within the stated scope.")
    if case["family"] in SERVICING_VARIANTS and case["world"]["scope"]:
        # The app and the customer must deliver the same decision on the confirmation turn.
        fixed.update(enumerate(case["messages"][1:], start=1))
    if case["family"] in CARD_VARIANTS:
        for i in range(len(stages)):
            stages[i] += " Verified personal details and signed permissions come only from the app."
        if case["difficulty"] != "easy" and case["family"] != "product_qa":
            fixed[len(stages) - 1] = case["messages"][-1]
    if case["family"] in ("evidence", "handoff"):
        fixed[1] = case["messages"][1]
    if case["family"] == "routing":
        stages[0] += " Give only the facts in your objective. Do not suggest a remedy or authorize a case change."
        facts = {"unauthorised_activity": FRAUD_REPORT, "recognised_billing": RECOGNISED_REPORT}
        if case["variant"] in facts:
            required[0] = facts[case["variant"]]
        fixed.update(dict(enumerate(case["messages"][1:], 1)))
    if case.get("conditional_withdrawal"):
        fixed = dict(enumerate(case["messages"]))
    elif case["family"] == "refund":
        required[1] = REFUND_AUTHORIZATION
    if case["family"] == "followup":
        policy, _ = FOLLOWUP_VARIANTS[case["variant"]]
        opening = "Please end your reply with 'Outstanding: GBP' followed by the amount. " + OPENING_INSTRUCTION
        required.update({0: opening, 1: CORRECTIONS[policy]})
        fixed = dict(enumerate(case["messages"][2:], 2))
    for i, consent in required.items():
        stages[i] += " Include this wording exactly: " + consent
    brief = ("Role-play the customer in a bank chat. Return one short message. "
             "Only use facts in your objective or the public dialogue.")
    if today := service_date_instruction(case):
        brief += " " + today
    return ScenarioCustomer(factory(), brief,
                            PERSONA[case["behaviour"]], stages, directory / "customers", required, fixed)


def write(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def source_digest():
    root = Path(__file__).resolve().parents[1]
    sources = [p for directory in ("harness", "adapters", "bank_demo", "docs/policies")
               for p in (root / directory).rglob("*") if p.suffix in (".py", ".toml", ".md")]
    sources.extend(root / "evaluation" / name for name in ("__init__.py", "casebooks.py", "scoring.py"))
    return digest({p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sources})


def stage_report(report):
    book = json.loads(report.with_name("casebook.json").read_text())
    validate(book)
    cases = {case["case_id"]: case for case in book["cases"]}
    results = []
    for row in json.loads(report.read_text()):
        case = cases[row["case_id"]]
        if len(case.get("stages", [])) < 2:
            continue
        stages = {}
        for stage in case["stages"]:
            checks = [Check(**c) for c in row["checks"] if stage in c["criterion"].split(" · ")[:-1]]
            status, axes = scores(checks) if checks else ("UNASSESSED", {})
            if row["status"] == "ERROR":
                first_finished = any(c["criterion"].startswith("Turn 1 · product_qa · Completes the customer turn")
                                     and c["passed"] is True for c in row["checks"])
                failed_stage = case["stages"][-1] if first_finished else case["stages"][0]
                if stage == failed_stage:
                    status, axes = "ERROR", {}
            stages[stage] = {"status": status, **axes}
        results.append({"case_id": row["case_id"], "repeat": row["repeat"],
                        "status": row["status"], "stages": stages})
    return results


def score_case(case, directory, customer_mode, instruction, factory, *, use_mailbox=False):
    task = materialize(case, factory, instruction)
    if use_mailbox:
        from .env0_mail import mailbox_task
        task = mailbox_task(task)
    customer = actor(case, directory, factory) if customer_mode == "llm" else None
    outcome = evaluate(task, directory, customer=customer)
    return usage_scorecard(outcome, customer)


def run_trial(case, repeat, batch, customer_mode, instruction, backend):
    relative = Path("trials") / case["case_id"] / str(repeat)
    directory = batch / relative
    try:
        directory.mkdir(parents=True)
        if backend == "benchflow":
            from .benchflow_trial import run_in_sandbox
            scored = run_in_sandbox(case, directory, customer_mode, instruction)
        else:
            scored = score_case(case, directory, customer_mode, instruction, create_model)
    except Exception as error:
        scored = {**asdict(Outcome(case["variant"], "ERROR", error=f"{type(error).__name__}: {error}",
                                   dimensions=scores([], error=True)[1])),
                  "customer_audits": []}
    dimensions = {k: case[k] for k in ("case_id", "family", "variant", "behaviour", "difficulty", "world_seed")}
    return {**scored, **dimensions, "repeat": repeat, "trial_dir": str(relative), "customer_mode": customer_mode}


def run(book, args):
    validate(book)
    if args.repeats < 1:
        raise ValueError("repeats must be positive")
    backend = getattr(args, "backend", "benchflow")
    workers = args.workers if args.workers is not None else (8 if backend == "benchflow" else 1)
    if workers < 1:
        raise ValueError("workers must be positive")
    runtime = {}
    if backend == "benchflow":
        from .benchflow_trial import preflight
        runtime = preflight()
    instruction = args.instructions.read_text() if args.instructions else ""
    directory = args.output / uuid4().hex
    directory.mkdir(parents=True)
    write(directory / "casebook.json", book)
    model = create_model()
    manifest = {"suite": "generated", "arm": args.arm, "seed": book["seed"], "split": book["split"],
                "generator_version": book["version"], "score_version": SCORE_VERSION,
                "bank_prompt_version": BANK_PROMPT_VERSION, "policy_sha256": POLICY_SHA256,
                "casebook_id": digest(book), "instruction_hash": digest(instruction),
                "source_sha256": source_digest(),
                "extra_instruction": instruction, "customer_mode": args.customer_mode,
                "actor_version": 2 if any(c["family"] == "followup" for c in book["cases"]) else 1,
                "repeats": args.repeats, "workers": workers, "backend": backend, **runtime, "model": model.model_id,
                "reasoning_effort": model.reasoning_effort, "price_card": price_card()}
    manifest["max_tool_calls"] = DEFAULT_MAX_TOOL_CALLS
    if any(c["family"] in SERVICING_VARIANTS for c in book["cases"]):
        manifest["actor_version"] = 3
    if any(service_date_instruction(c) for c in book["cases"]):
        manifest["session_date_source"] = "frozen_case"
    budget = getattr(model, "context_budget", None)
    if not isinstance(budget, ContextBudget):
        budget = ContextBudget()
    manifest.update(window=budget.window, reserve=budget.reserve, unit=budget.unit)
    timeout = getattr(model, "timeout_seconds", None)
    if isinstance(timeout, (int, float)):
        manifest["model_timeout_seconds"] = timeout
    if any(c["family"] == "card_selection" for c in book["cases"]):
        manifest["card_selection_version"] = CARD_SELECTION_VERSION
    write(directory / "manifest.json", manifest)
    completed = {}
    report = directory / "report.json"
    trials = ((case, repeat) for repeat in range(1, args.repeats + 1) for case in book["cases"])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(run_trial, case, repeat, directory, args.customer_mode, instruction, backend): index
                   for index, (case, repeat) in enumerate(trials)}
        for future in as_completed(pending):
            row = future.result()
            row["estimated_cost_usd"] = estimate_cost(row, manifest)
            completed[pending[future]] = row
            rows = [completed[i] for i in sorted(completed)]
            staging = report.with_suffix(".tmp")
            write(staging, rows)
            staging.replace(report)
            cost = row["estimated_cost_usd"].get("total")
            spend = f"${cost:.6f}" if cost is not None else "unknown"
            print(args.arm, row["case_id"][:12], row["repeat"], row["task"], row["status"],
                  f"estimated cost {spend}", flush=True)
    print(report, flush=True)
    publish_if_configured(report)
    return exit_code(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("stages", help="Read stage verdicts from an execution or reviewed report").add_argument("report", type=Path)
    gen = commands.add_parser("generate")
    gen.add_argument("file", type=Path)
    gen.add_argument("--seed", type=int, required=True)
    gen.add_argument("--split", choices=("development", "holdout"), default="development")
    gen.add_argument("--family", choices=tuple(VARIANTS), nargs="+")
    gen.add_argument("--prepend", choices=("product_qa",), help="Open each upsell or eligibility case with product Q&A")
    gen.add_argument("--variant", choices=[v for group in VARIANTS.values() for v in group], nargs="+")
    gen.add_argument("--behaviour", choices=BEHAVIOURS, nargs="+", default=BEHAVIOURS)
    gen.add_argument("--difficulty", choices=DIFFICULTIES, nargs="+", default=DIFFICULTIES)
    counts = gen.add_mutually_exclusive_group()
    counts.add_argument("--per-cell", type=int, default=1,
                        help="Instances per variant × behaviour × difficulty (default: 1)")
    counts.add_argument("--worlds-per-family", type=int,
                        help="Independent worlds per family, spread across outcomes and difficulty levels")
    gen.add_argument("--sampling", choices=("balanced", "iid"),
                     help="With --worlds-per-family, balance cells (default) or draw each case independently from the uniform mix")
    gen.add_argument("--extended-followups", action="store_true",
                     help="Add purchase comparison and credit tracing to hard corrected-consent conversations")
    transfer = commands.add_parser("transfer", help="Freeze a completed live open-draft conversation for the next agent")
    transfer.add_argument("directory", type=Path)
    transfer.add_argument("file", type=Path)
    transfer.add_argument("--case-id")
    transfer.add_argument("--repeat", type=int, default=1)
    execute = commands.add_parser("run")
    execute.add_argument("file", type=Path)
    execute.add_argument("--arm", required=True)
    execute.add_argument("--instructions", type=Path, help="Additional bank instructions for this arm")
    execute.add_argument("--customer-mode", choices=("scripted", "llm"), default="scripted")
    execute.add_argument("--repeats", type=int, default=1)
    execute.add_argument("--workers", type=int, help="Maximum concurrent independent trials (BenchFlow: 8, local: 1)")
    execute.add_argument("--backend", choices=("benchflow", "local"), default="benchflow",
                         help="Trial environment (default: benchflow). Use local for in-process debugging.")
    execute.add_argument("--output", type=Path, default=Path("audits/evaluations"))
    args = parser.parse_args()
    if args.command == "stages":
        print(json.dumps(stage_report(args.report), indent=2))
        return 0
    if args.command == "run":
        return run(json.loads(args.file.read_text()), args)
    if args.command == "transfer":
        if args.file.exists() and args.file.resolve().is_relative_to(args.directory.resolve()):
            parser.error("Choose a new casebook file to preserve the source execution")
        book = freeze_handoff(args.directory, args.case_id, args.repeat)
    else:
        families = args.family or (["upsell", "eligibility"] if args.prepend else list(VARIANTS))
        if args.sampling and args.worlds_per_family is None:
            parser.error("--sampling requires --worlds-per-family")
        try:
            generator = generate_independent if args.worlds_per_family is not None else generate
            count = args.worlds_per_family if args.worlds_per_family is not None else args.per_cell
            options = {"sampling": args.sampling} if args.sampling else {}
            book = generator(args.seed, args.split, families, args.behaviour, args.difficulty, count, args.variant,
                             extended_followups=args.extended_followups, prepend=args.prepend, **options)
        except ValueError as error:
            parser.error(str(error))
    if not book["cases"]:
        parser.error("No outcome variants match the selected families")
    validate(book)
    args.file.parent.mkdir(parents=True, exist_ok=True)
    write(args.file, book)
    print(f"{len(book['cases'])} cases · {digest(book)} · {args.file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
