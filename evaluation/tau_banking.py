"""Run the frozen two-task τ-Banking pilot using a signed-in Codex CLI."""
import argparse
import json
import os
import subprocess
import sys
import tarfile
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch
from uuid import uuid4

from adapters.codex_cli import CodexCLI
from harness import Reply
from harness.usage import capture_usage
from .usage import estimate_cost, price_card


REVISION = "fc0055dc4e0a316c3f83133267fbd6faaa770992"  # Sierra v1.0.1
TASKS = ["task_001", "task_002"]


def summary(results):
    simulations = results["simulations"]
    observed = {(s["task_id"], s["trial"]) for s in simulations}
    if observed != {(task, trial) for task in TASKS for trial in range(2)} or len(simulations) != 4:
        raise ValueError("Expected two attempts on each frozen task")
    rows = []
    for sim in sorted(simulations, key=lambda s: (s["task_id"], s["trial"])):
        bank_messages = [m for m in sim["messages"] or [] if m["role"] == "assistant" and m.get("usage")]
        rows.append({"task": sim["task_id"], "attempt": sim["trial"] + 1,
                     "reward": (sim.get("reward_info") or {}).get("reward"), "stop": sim["termination_reason"],
                     "bank_model_calls_with_usage": len(bank_messages),
                     "bank_estimated_usd": sim.get("agent_cost"), "customer_estimated_usd": sim.get("user_cost")})
    return {"first_attempt_passes": sum(r["attempt"] == 1 and r["reward"] == 1 for r in rows),
            "passed_every_repeat": sum(all(r["reward"] == 1 for r in rows if r["task"] == task) for task in TASKS),
            "tasks": len(TASKS), "attempts": rows}


def run(output):
    from tau2 import TextRunConfig
    from tau2.agent import llm_agent
    from tau2.data_model.message import AssistantMessage, ToolCall
    from tau2.runner.batch import run_domain
    from tau2.user import user_simulator
    from tau2.utils.llm_utils import to_litellm_messages

    client = CodexCLI()
    # The external benchmark supplies its own policy, retrieval results and history.
    client.context_budget = replace(client.context_budget, window=512 * 1024)
    pricing = {"model": client.model_id, "price_card": price_card()}
    config = TextRunConfig(domain="banking_knowledge", task_ids=TASKS, num_trials=2, seed=300,
                           llm_agent=client.model_id, llm_user=client.model_id,
                           llm_args_agent={"reasoning_effort": client.reasoning_effort},
                           llm_args_user={"reasoning_effort": client.reasoning_effort},
                           retrieval_config="bm25", max_concurrency=4, max_retries=0,
                           auto_review=False, save_to=str(output))
    manifest = {"release": REVISION, "config": config.model_dump(mode="json"),
                "external_adapter_window_bytes": client.context_budget.window, **pricing}
    output.mkdir(parents=True, exist_ok=False)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    def generate(model, messages, tools=None, **kwargs):
        if model != client.model_id or kwargs.get("reasoning_effort") != client.reasoning_effort:
            raise ValueError("Unexpected model configuration")
        schemas = {t.name: t.openai_schema["function"] for t in tools or []}
        started = monotonic()
        with capture_usage() as metadata:
            action = client.respond(to_litellm_messages(messages), schemas)
        reported = metadata.get("usage")
        cost = usage = None
        if reported:
            counts = {"calls": 1, "reported_calls": 1, **reported}
            cost = estimate_cost({"token_usage": {"bank": counts}}, pricing).get("bank")
            usage = {"prompt_tokens": reported["input_tokens"], "completion_tokens": reported["output_tokens"],
                     **reported}
        is_reply = isinstance(action, Reply)
        return AssistantMessage(role="assistant", content=action.text if is_reply else None,
                                tool_calls=None if is_reply else [ToolCall(id=uuid4().hex, name=action.name,
                                                                          arguments=action.arguments)],
                                cost=cost, usage=usage, generation_time_seconds=monotonic() - started)

    with patch.object(llm_agent, "generate", generate), patch.object(user_simulator, "generate", generate):
        results = run_domain(config)
    (output / "summary.json").write_text(json.dumps(summary(results.model_dump()), indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="Local Sierra tau2-bench git repository")
    parser.add_argument("output", type=Path, help="New result directory")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="bank-tau-") as scratch:
        source = Path(scratch)
        archive = source / "release.tar"
        with archive.open("wb") as target:
            subprocess.run(["git", "-C", str(args.checkout), "archive", REVISION, "src", "data/tau2"],
                           stdout=target, check=True)
        with tarfile.open(archive) as tar:
            tar.extractall(source, filter="data")
        sys.path.insert(0, str(source / "src"))
        os.environ["TAU2_DATA_DIR"] = str(source / "data")
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        run(args.output.resolve())
