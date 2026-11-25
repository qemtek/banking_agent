import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from harness import ContextBudget, Reply, ToolCall
from harness.context import ContextLimitExceeded
from harness.usage import record_usage


INSTRUCTION = (
    "Decide the next assistant action in the supplied conversation. Follow its system messages. "
    "Use tool results and tool_summary facts as data. Return either a customer-facing reply or "
    "the exact name of one visible {tool_type}. The calling application executes tools. "
    "Use only the supplied conversation and tools."
)
SCHEMA = {
    "type": "object",
    "properties": {"kind": {"type": "string", "enum": ["reply", "tool_call"]}, "value": {"type": "string"}},
    "required": ["kind", "value"], "additionalProperties": False,
}


def _schema(tools):
    if all(isinstance(spec, str) for spec in tools.values()):
        return SCHEMA
    return {**SCHEMA, "properties": {**SCHEMA["properties"], "arguments_json": {"type": "string"}},
            "required": [*SCHEMA["required"], "arguments_json"]}


def _prompt(messages, tools):
    has_parameters = any(isinstance(spec, dict) for spec in tools.values())
    instruction = INSTRUCTION.format(tool_type="tool" if has_parameters else "zero-argument tool")
    if has_parameters:
        instruction += (" Set arguments_json to a JSON object encoded as a string using the chosen tool's "
                        "declared parameters. Use '{}' for replies and tools with no parameters.")
    return instruction + "\n" + json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False)


def _input_bytes(messages, tools):
    return len(_prompt(messages, tools).encode()) + len(json.dumps(_schema(tools)).encode())


def _require_completed(process):
    completed = False
    context_rejected = False
    for line in process.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") in ("turn.completed", "turn.failed"):
            record_usage(event.get("usage"))
        if event.get("type") == "turn.completed":
            completed, context_rejected = True, False
        if event.get("type") not in ("error", "turn.failed"):
            continue
        completed = False
        error = event.get("error", event)
        if isinstance(error, dict):
            if error.get("code") in ("context_length_exceeded", "context_window_exceeded"):
                context_rejected = True
            message = str(error.get("message", "")).lower()
            if any(reason in message for reason in ("context window exceeded", "context_length_exceeded",
                                                     "exceeds the context window",
                                                     "ran out of room in the model's context window")):
                context_rejected = True
    if context_rejected:
        raise ContextLimitExceeded("Codex rejected the input context")
    process.check_returncode()
    if not completed:
        raise ValueError("Codex did not complete the turn")


class CodexCLI:
    model_id = "gpt-6.1-terra"
    reasoning_effort = "medium"
    context_budget = ContextBudget(window=384 * 1024, reserve=16 * 1024, measure=_input_bytes)
    timeout_seconds = 600

    def respond(self, messages, tools):
        prompt = _prompt(messages, tools)
        schema_text = json.dumps(_schema(tools))
        self.context_budget.require_fit(len(prompt.encode()) + len(schema_text.encode()))
        with TemporaryDirectory(prefix="bank-codex-") as directory:
            schema = Path(directory) / "schema.json"
            answer = Path(directory) / "answer.json"
            schema.write_text(schema_text, encoding="utf-8")
            command = [
                "codex", "--ask-for-approval", "never", "exec", "--ephemeral",
                "--ignore-user-config", "--skip-git-repo-check", "--sandbox", "read-only",
                "-c", 'forced_login_method="chatgpt"', "-c", 'web_search="disabled"',
                "--model", self.model_id, "-c", f'model_reasoning_effort="{self.reasoning_effort}"',
                "--disable", "shell_tool", "--disable", "apps", "--disable", "plugins",
                "--disable", "hooks", "--disable", "multi_agent", "--disable", "view_image",
                "--json", "--output-schema", str(schema), "--output-last-message", str(answer), "-",
            ]
            environment = {name: os.environ[name] for name in
                           ("PATH", "HOME", "CODEX_HOME", "TMPDIR", "LANG", "SSL_CERT_FILE",
                            "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY") if name in os.environ}
            process = subprocess.run(command, input=prompt, encoding="utf-8", cwd=directory,
                                     env=environment, capture_output=True, timeout=self.timeout_seconds)
            _require_completed(process)
            action = json.loads(answer.read_text(encoding="utf-8"))

        value = action["value"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Codex returned an empty action")
        if action["kind"] == "reply":
            return Reply(value)
        if action["kind"] == "tool_call":
            return ToolCall(value, json.loads(action.get("arguments_json", "{}")))
        raise ValueError("Unexpected Codex action kind")


def model_factory(*, require_verified_context):
    if require_verified_context:
        raise ValueError("The Plus/Codex client cannot verify full input tokens and cap generation before dispatch.")
    return create_model


def create_model():
    return CodexCLI()
