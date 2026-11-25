"""BenchFlow runs the bank and grader in Docker. Codex decisions run on the signed-in host."""
import asyncio
import json
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from adapters.codex_cli import CodexCLI, create_model
from harness import Reply, ToolCall
from harness.context import ContextLimitExceeded
from harness.usage import capture_usage, record_usage

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
MAIL_MANIFEST = Path(__file__).with_name("env0-mail.toml")
EXCHANGE = Path("/logs/agent")


def preflight():
    try:
        from benchflow.sandbox.docker import DockerSandbox
    except ImportError as error:
        raise SystemExit("Install requirements-benchflow.txt in the Python environment running the casebook.") from error
    DockerSandbox.preflight()
    from benchflow.environment import load_manifest
    return {"benchflow_version": version("benchflow"), "sandbox_image": IMAGE,
            "env0_image": load_manifest(MAIL_MANIFEST).base_image}


def publish(path, payload):
    staging = path.with_suffix(".tmp")
    staging.write_text(json.dumps(payload))
    staging.replace(path)


class HostDecision(CodexCLI):
    def respond(self, messages, tools):
        publish(EXCHANGE / "request.json", {"messages": messages, "tools": tools})
        response = EXCHANGE / "response.json"
        deadline = monotonic() + self.timeout_seconds + 40
        while not response.exists():
            if monotonic() > deadline:
                raise TimeoutError("Host model response timed out")
            sleep(0.05)
        action = json.loads(response.read_text())
        response.unlink()
        record_usage(action.pop("usage", None))
        if "error" in action:
            if action["context_limit"]:
                raise ContextLimitExceeded(action["error"])
            raise RuntimeError(action["error"])
        return Reply(**action) if "text" in action else ToolCall(**action)


def worker():
    from .casebook import score_case
    inputs = json.loads((EXCHANGE / "case.json").read_text())
    scored = score_case(directory=Path("/trial"), factory=HostDecision, **inputs)
    publish(EXCHANGE / "result.json", scored)


async def execute(case, directory, customer_mode, instruction):
    from benchflow.environment import ManifestEnvironment, load_manifest
    from benchflow.sandbox.docker import DockerSandbox
    from benchflow.task.config import SandboxConfig
    from benchflow.task.paths import RolloutPaths

    paths = RolloutPaths(directory / "benchflow")
    paths.mkdir()
    mounts = [{"type": "bind", "source": str(ROOT / name), "target": "/app/" + name, "read_only": True}
              for name in ("bank_demo", "adapters", "evaluation", "harness", "docs/policies")]
    mounts.append({"type": "bind", "source": str(directory.resolve()), "target": "/trial"})
    mail = load_manifest(MAIL_MANIFEST) if case["family"] in ("evidence", "handoff", "routing") else None
    image = mail.base_image if mail else IMAGE
    sandbox = DockerSandbox(environment_dir=paths.rollout_dir, environment_name="bank",
                            session_id="bank-" + uuid4().hex, rollout_paths=paths, mounts_json=mounts,
                            task_env_config=SandboxConfig(docker_image=image, network_mode="no-network"))
    exchange = paths.agent_dir
    publish(exchange / "case.json", {"case": case, "customer_mode": customer_mode, "instruction": instruction,
                                     "use_mailbox": mail is not None})
    model = create_model()
    try:
        await sandbox.start(force_build=False)
        if mail:
            seed = int(case["world_seed"])
            seeded = await sandbox.exec(f"mock-gmail --db /trial/mail.sqlite seed --seed {seed}", timeout_sec=90)
            if seeded.return_code:
                raise RuntimeError(f"Mailbox seed failed: {seeded.stderr or seeded.stdout}")
            services = ManifestEnvironment(mail, sandbox=sandbox)
            await services.provision(None)
            ready = await services.readiness()
            if not ready.ready:
                raise RuntimeError(ready.error)
        execution = asyncio.create_task(sandbox.exec("python -B -m bank_demo.benchflow_trial", cwd="/app",
                                                     timeout_sec=3600))
        try:
            while not execution.done():
                request = exchange / "request.json"
                if request.exists():
                    data = json.loads(request.read_text())
                    request.unlink()
                    with capture_usage() as metadata:
                        try:
                            reply = await asyncio.to_thread(model.respond, data["messages"], data["tools"])
                            answer = asdict(reply)
                        except Exception as error:
                            answer = {"error": str(error), "context_limit": isinstance(error, ContextLimitExceeded)}
                    publish(exchange / "response.json", {**answer, **metadata})
                await asyncio.wait((execution,), timeout=0.05)
            finished = await execution
            if finished.return_code:
                raise RuntimeError(f"BenchFlow worker exited {finished.return_code}: {finished.stderr or finished.stdout}")
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
    finally:
        await sandbox.stop(delete=False)
    scored = json.loads((exchange / "result.json").read_text())
    scored["sandbox_image"] = image
    scored["services"] = ["mock-gmail"] if mail else []
    for key in ("turn_audits", "customer_audits"):
        scored[key] = [str(directory / Path(p).relative_to("/trial")) for p in scored[key]]
    if scored["audit"]:
        scored["audit"] = str(directory / Path(scored["audit"]).relative_to("/trial"))
    return scored


def run_in_sandbox(case, directory, customer_mode, instruction):
    return asyncio.run(execute(case, directory, customer_mode, instruction))


if __name__ == "__main__":
    worker()
