"""Local MLflow tracking for completed evaluations and recovery experiments."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .casebooks import trial_directory
from .usage import report_cost


ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "audits" / "mlflow"
DATABASE = STORE / "tracking.db"
EXPERIMENT = "Bank agent evaluations"
PARAMS = ("suite", "seed", "model", "reasoning_effort", "backend", "benchflow_version",
          "bank_prompt_version", "policy_sha256", "score_version",
          "fixture_version", "ledger_environment", "window", "reserve", "unit", "context_mode", "max_tool_calls",
          "model_timeout_seconds", "session_date_source", "source_sha256")


def publish_if_configured(report):
    if not DATABASE.exists():
        return
    python = ROOT / ".venv" / "bin" / "python"
    command = [str(python) if python.exists() else sys.executable,
               "-m", "evaluation.tracking", "import", str(report.resolve())]
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode:
        print(f"MLflow export failed. The local report is saved. Retry with {' '.join(command)}", file=sys.stderr)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def turn_limit_metrics(paths):
    if not paths or not all(path.is_file() for path in paths):
        return {}
    requests = []
    stops = []
    for path in paths:
        with path.open(encoding="utf-8") as source:
            events = [json.loads(line) for line in source]
        if not events or events[-1]["event_type"] != "run.finished":
            return {}
        requests.append(sum(event["event_type"] == "tool.requested" for event in events))
        stops.append(events[-1]["payload"]["reason"])
    return {"bank_tool_requests": sum(requests), "max_tool_requests_per_turn": max(requests),
            "tool_limit_stops": stops.count("call_limit"), "context_limit_stops": stops.count("context_limit")}


def evaluation_metrics(item):
    metrics = {"checks_count": len(item["checks"])}
    usage = {f"{actor}_{key}": value for actor, counts in item.get("token_usage", {}).items()
             for key, value in counts.items()}
    grouped = defaultdict(list)
    for check in item["checks"]:
        if check["passed"] is None:
            continue
        passed = int(check["passed"])
        if check.get("category"):
            grouped[check["category"]].append(passed)
        criterion = check["criterion"]
        if criterion == "Turn 1 · Outstanding amount belongs to the disputed charge":
            metrics["initial_amount_correct"] = passed
        elif criterion == "Turn 1 · Inspected the draft and statement before reporting the refund":
            metrics["initial_evidence_checked"] = passed
        elif criterion.endswith("Correct action and destination proposed"):
            metrics["routing_decision_correct"] = min(passed, metrics.get("routing_decision_correct", 1))
        elif criterion.endswith("Correct specialist queue, priority and evidence"):
            metrics["routing_queue_correct"] = min(passed, metrics.get("routing_queue_correct", 1))
        elif criterion.endswith("Exact committed ledger writes"):
            # The last recorded turn describes the cumulative mutation journal.
            metrics["committed_writes_correct"] = passed
    for category, values in grouped.items():
        metrics[category + "_checks_pass_rate"] = sum(values) / len(values)
    if item["status"] == "ERROR":
        # Partial checks remain in the scorecard for diagnosis.
        return {"checks_count": metrics["checks_count"], **usage}
    for axis, status in item.get("dimensions", {}).items():
        metrics[axis + "_assessed"] = int(status in ("PASS", "FAIL"))
        if status in ("PASS", "FAIL"):
            metrics[axis + "_correct"] = int(status == "PASS")
    if item.get("family") == "followup" and item.get("turn_audits"):
        turns = len(item["turn_audits"])
        checks = {check["criterion"]: check["passed"] for check in item["checks"]}
        required = [f"Turn {n} · Proposed exactly the authorized mutations" for n in range(1, turns + 1)]
        required.extend(f"Turn {turns} · {name}" for name in (
            "Completes the customer turn", "Reports saved case, charge and status",
            "Outstanding amount belongs to the disputed charge", "Checks current evidence",
            "Saved case matches the correction", "Exact committed ledger writes",
        ))
        if all(checks.get(name) is not None for name in required):
            metrics["corrected_resolution"] = int(all(checks[name] for name in required))
    return {**metrics, **usage}


def evaluation_entries(directory, rows):
    manifest_path = directory / "manifest.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else {}
    for item in rows:
        mode = item.get("customer_mode", "scripted")
        params = {k: manifest[k] for k in PARAMS if k in manifest}
        params["customer_mode"] = mode
        if "score_version" in item:
            params["score_version"] = item["score_version"]
        if "case_id" in item:
            params.update({k: manifest[k] for k in ("arm", "split", "casebook_id", "instruction_hash", "generator_version")})
            params.update({k: item[k] for k in ("case_id", "family", "variant", "behaviour", "difficulty", "world_seed", "repeat")})
        if item.get("customer_fidelity"):
            params["customer_fidelity"] = item["customer_fidelity"]
        if item.get("reviewer"):
            params["reviewer"] = item["reviewer"]
        if mode == "llm":
            params.update({"customer_" + k: v for k, v in manifest.get("customer", {}).items()})
        trial = trial_directory(directory, item)
        bank_audits = [trial / Path(saved).name for saved in item.get("turn_audits") or [item.get("audit")]
                       if saved]
        files = bank_audits.copy()
        files.extend(trial / "customers" / Path(p).name for p in item.get("customer_audits", []))
        for check in item["checks"]:
            match = re.match(r"(ledger-[0-9a-f]+\.sqlite):", str(check.get("evidence", "")))
            if match:
                files.append(trial / match[1])
        metrics = evaluation_metrics(item)
        if item["status"] != "ERROR":
            metrics.update(turn_limit_metrics(bank_audits))
        cost = report_cost(item, manifest)
        if cost:
            params["price_card"] = cost["price_card"]
            for key, metric in (("bank", "bank_estimated_cost_usd"), ("customer", "customer_estimated_cost_usd"),
                                ("total", "estimated_cost_usd")):
                if key in cost:
                    metrics[metric] = cost[key]
        yield item["task"], item["status"], params, metrics, files, item


def recovery_entries(directory, report):
    for task, item in report["cases"].items():
        status = "ERROR" if item.get("error") else "PASS" if all(item["checks"].values()) else "FAIL"
        params = {"suite": "recovery", "seed": report["seed"], "model": report["model"],
                  "reasoning_effort": report["effort"], "customer_mode": "scripted"}
        metrics = {k: int(v) for k, v in item["checks"].items()} if status != "ERROR" else {}
        if "writes" in item:
            metrics["committed_write_count"] = len(item["writes"])
        case_dir = directory / task
        files = list((case_dir / "audit").glob("*.jsonl")) + list(case_dir.glob("*.json"))
        if (case_dir / "bank.sqlite").is_file():
            files.append(case_dir / "bank.sqlite")
        yield task, status, params, metrics, files, item


def audit_metadata(files, fallback_time):
    times = []
    model = {}
    for path in files:
        if path.suffix != ".jsonl" or not path.is_file():
            continue
        with path.open() as source:
            for line in source:
                event = json.loads(line)
                if event.get("timestamp"):
                    times.append(int(datetime.fromisoformat(event["timestamp"]).timestamp() * 1000))
                if event.get("event_type") == "run.started":
                    model.update({k: event["payload"][k] for k in ("model", "reasoning_effort") if k in event["payload"]})
    return min(times, default=fallback_time), max(times, default=fallback_time), model


def import_report(client, experiment_id, report):
    directory = report.parent
    data = read_json(report)
    recovery = isinstance(data, dict) and "cases" in data
    kind = "recovery" if recovery else "evaluation"
    entries = recovery_entries(directory, data) if recovery else evaluation_entries(directory, data)
    created = skipped = 0
    for index, (task, status, params, metrics, files, scorecard) in enumerate(entries):
        identity = scorecard.get("trial_dir", index)
        source = f"{kind}/{directory.name}/{identity}/{task}/{params['customer_mode']}"
        execution_key = hashlib.sha256(source.encode()).hexdigest()
        derived = report.name != "report.json" and not recovery and "dimensions" in scorecard
        if derived:
            source += f"/{report.name}/{hashlib.sha256(json.dumps(scorecard, sort_keys=True).encode()).hexdigest()}"
        key = hashlib.sha256(source.encode()).hexdigest()
        existing = client.search_runs([experiment_id], f"tags.source_key = '{key}'", max_results=1)
        exported = existing and existing[0].data.tags.get("export_complete") == "true"
        if exported and "price_card" in params and "price_card" not in existing[0].data.params:
            from mlflow.entities import Metric, Param
            run = existing[0]
            client.log_batch(run.info.run_id, metrics=[
                Metric(k, v, run.info.end_time, 0) for k, v in metrics.items() if k.endswith("estimated_cost_usd")
            ], params=[Param("price_card", params["price_card"])])
        if exported and (derived or existing[0].data.tags.get("turn_traces_version") == "1"):
            skipped += 1
            continue
        files = list(dict.fromkeys(files))
        if any(not p.resolve().is_relative_to(directory.resolve()) for p in files):
            raise ValueError(f"Evidence outside report directory: {report}")
        if exported:
            run_id = existing[0].info.run_id
            skipped += 1
        else:
            start, end, model = audit_metadata(files, int(report.stat().st_mtime * 1000))
            tags = {"task": task, "eval_status": status, "report_type": kind, "source_key": key,
                    "source_batch": directory.name, "source_report": str(report), **{
                        k: str(v) for k, v in params.items() if k in ("customer_mode", "customer_persona", "seed")}}
            tags["measurement"] = "regrade" if derived else "execution"
            if derived:
                originals = client.search_runs([experiment_id], f"tags.source_key = '{execution_key}'", max_results=1)
                if originals:
                    tags["execution_run_id"] = originals[0].info.run_id
            run_id = (existing[0] if existing else client.create_run(
                experiment_id, start_time=start, tags=tags, run_name=f"{task} · {params['customer_mode']}")).info.run_id
            from mlflow.entities import Metric, Param
            client.log_batch(run_id, metrics=[Metric(k, v, end, 0) for k, v in metrics.items()],
                             params=[Param(k, str(v)) for k, v in {**model, **params}.items()])
            client.log_dict(run_id, scorecard, "scorecard.json")
            if scorecard.get("dialogue"):
                client.log_dict(run_id, {"messages": scorecard["dialogue"]}, "dialogue.json")
            manifest = directory / "manifest.json"
            if manifest.is_file():
                client.log_artifact(run_id, str(manifest))
            for path in files:
                if path.is_file():
                    parent = Path("evidence") / path.relative_to(directory).parent
                    client.log_artifact(run_id, str(path), str(parent))
            client.set_terminated(run_id, "FAILED" if status == "ERROR" else "FINISHED", end_time=end)
            client.set_tag(run_id, "export_complete", "true")
            created += 1
        if derived:
            continue
        from evaluation.traces import export_turns
        import mlflow
        # The explicit-time span API uses MLflow's process-wide tracking destination.
        previous_uri = mlflow.get_tracking_uri()
        try:
            mlflow.set_tracking_uri(client.tracking_uri)
            count = export_turns(client, experiment_id, run_id, task, params["customer_mode"], files)
        finally:
            mlflow.set_tracking_uri(previous_uri)
        client.set_tag(run_id, "turn_traces", str(count))
        client.set_tag(run_id, "turn_traces_version", "1")

    return created, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backfill = commands.add_parser("import", help="Import one report or all saved project reports")
    backfill.add_argument("report", type=Path, nargs="?")
    ui = commands.add_parser("ui", help="Open the loopback tracking server")
    ui.add_argument("--port", type=int, default=5055)
    args = parser.parse_args()
    STORE.mkdir(parents=True, exist_ok=True)
    uri = f"sqlite:///{DATABASE}"
    if args.command == "ui":
        os.execv(sys.executable, [sys.executable, "-m", "mlflow", "server", "--host", "127.0.0.1",
                                  "--port", str(args.port), "--workers", "1", "--backend-store-uri", uri,
                                  "--no-serve-artifacts", "--default-artifact-root", (STORE / "artifacts").as_uri()])
    import mlflow
    from mlflow import MlflowClient
    mlflow.set_tracking_uri(uri)
    reports = [args.report.resolve()] if args.report else sorted(
        p for name in ("evaluations", "recovery") for p in (ROOT / "audits" / name).glob("*/report.json"))
    with (STORE / "import.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        client = MlflowClient(tracking_uri=uri)
        experiment = client.get_experiment_by_name(EXPERIMENT)
        experiment_id = experiment.experiment_id if experiment else client.create_experiment(
            EXPERIMENT, artifact_location=(STORE / "artifacts").as_uri())
        created = skipped = 0
        for report in reports:
            new, seen = import_report(client, experiment_id, report)
            created += new
            skipped += seen
    print(f"MLflow experiment {experiment_id}: {created} imported, {skipped} already tracked ({len(reports)} reports)")


if __name__ == "__main__":
    main()
