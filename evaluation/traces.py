"""Project recorded turns into MLflow without calling a model or a bank tool."""
import json
from datetime import datetime, timezone
from itertools import zip_longest

import mlflow
from mlflow.tracing.constant import SpanAttributeKey, TraceMetadataKey


# The call ID identifies a tool across middleware, review, retry and reconciliation.
START = {"model.requested": "model", "tool.started": "tool", "middleware.started": "middleware",
         "approval.requested": "approval", "context.summary_requested": "summary"}
END = {
    "model.responded": "model", "model.failed": "model", "generation.failed": "model",
    "tool.completed": "tool", "tool.failed": "tool", "middleware.decided": "middleware",
    "middleware.failed": "middleware", "context.summary_completed": "summary", "context.summary_failed": "summary",
    "approval.approved": "approval", "approval.declined": "approval", "approval.superseded": "approval",
    "approval.invalidated": "approval", "approval.unavailable": "approval",
}
FAILED_TURNS = {"model_error", "middleware_error", "context_error", "tool_error", "context_limit",
                "call_limit", "invalid_tool_call"}


def timestamp_ns(event):
    delta = datetime.fromisoformat(event["timestamp"]) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return ((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1000


def audit_rows(path):
    if path is None or not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def steps(events, actor):
    pending = {}
    commands = {}

    def step(start, end, group=None, incomplete=False):
        kind = start["event_type"]
        call = start.get("call_id")
        inputs = start["payload"]
        name = kind
        span_type = "CHAIN"
        if group in ("model", "summary"):
            name = f"{actor} model" if group == "model" else "summarize history"
            span_type = "LLM"
        elif group == "tool":
            name = inputs.get("name", "execute tool")
            inputs = commands.get(call, inputs)
            span_type = "TOOL"
        elif group == "middleware":
            name = f"{start['component']}.{inputs.get('hook', 'middleware')}"
        elif group == "approval":
            name = "review " + inputs.get("name", "proposal")
        attributes = {"actor": actor, "audit.start_event": kind, "audit.sequence": start["sequence"]}
        usage = end["payload"].get("usage")
        if span_type == "LLM" and usage and not incomplete:
            tokens = {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens")}
            if "cached_input_tokens" in usage:
                tokens["cache_read_input_tokens"] = usage["cached_input_tokens"]
            attributes[SpanAttributeKey.CHAT_USAGE] = tokens
            if "reasoning_output_tokens" in usage:
                attributes["reasoning_output_tokens"] = usage["reasoning_output_tokens"]
        if call:
            attributes["call_id"] = call
        if incomplete:
            # A crash can leave a start with no observed completion time.
            attributes["audit.completion"] = "unobserved"
        else:
            attributes["audit.end_event"] = end["event_type"]
        status = "UNSET" if incomplete else "ERROR" if end["event_type"].endswith((".failed", "_failed")) else "OK"
        return (name, span_type, inputs, None if incomplete else end["payload"], attributes,
                timestamp_ns(start), timestamp_ns(end), status)

    for event in events:
        kind = event["event_type"]
        if kind in ("run.started", "run.finished"):
            continue
        call = event.get("call_id")
        if kind == "tool.requested":
            commands[call] = event["payload"]
        group = START.get(kind) or END.get(kind)
        component = event["component"] if group == "middleware" else None
        key = (group, component, call)
        if kind in START:
            if previous := pending.get(key):
                yield step(previous, previous, group, incomplete=True)
            pending[key] = event
        elif kind in END and key in pending:
            yield step(pending.pop(key), event, group)
        else:
            yield step(event, event)
    for (group, _, _), event in pending.items():
        yield step(event, event, group, incomplete=True)


def export_turn(client, experiment_id, run_id, tags, customer, bank, session, turn):
    events = customer + bank
    start = bank[0]["payload"] if bank else {}
    finish = bank[-1]["payload"] if bank and bank[-1]["event_type"] == "run.finished" else {}
    reason = finish.get("reason", "customer_error" if not bank else "incomplete")
    message = start.get("message")
    if message is None and customer:
        message = next((e["payload"].get("text") for e in customer if e["event_type"] == "model.responded"), None)
    root = mlflow.start_span_no_context(
        f"{tags['task']} · turn {turn}", span_type="AGENT", experiment_id=experiment_id,
        start_time_ns=min(map(timestamp_ns, events)), inputs=message,
        tags={**tags, "turn": str(turn), "stop_reason": reason},
        metadata={TraceMetadataKey.SOURCE_RUN: run_id, TraceMetadataKey.TRACE_SESSION: session},
        attributes={"conversation_id": session, "turn_number": turn})
    count = 1
    for actor, rows in (("customer", customer), ("bank", bank)):
        for name, span_type, inputs, outputs, attrs, begin, end, status in steps(rows, actor):
            span = mlflow.start_span_no_context(name, span_type=span_type, parent_span=root,
                                               inputs=inputs, attributes=attrs, start_time_ns=begin)
            span.end(outputs=outputs, status=status, end_time_ns=end)
            count += 1
    status = "ERROR" if reason in FAILED_TURNS or reason == "customer_error" else "OK" if finish else "UNSET"
    root.end(outputs=finish.get("reply"), status=status, attributes={"stop_reason": reason},
             end_time_ns=max(map(timestamp_ns, events)))
    stored = client.get_trace(root.trace_id, display=False, flush=True)
    if len(stored.data.spans) != count:
        raise RuntimeError(f"Incomplete MLflow trace for {tags['audit_turn_id']}")


def export_turns(client, experiment_id, run_id, task, mode, paths):
    bank_paths = [p for p in paths if p.suffix == ".jsonl" and p.parent.name != "customers"]
    customer_paths = [p for p in paths if p.suffix == ".jsonl" and p.parent.name == "customers"]
    if not bank_paths and not customer_paths:
        return 0
    existing = client.search_traces(locations=[experiment_id], run_id=run_id, include_spans=False)
    seen = {t.info.tags.get("audit_turn_id") for t in existing}
    fallback_session = run_id
    count = 0
    for ordinal, (bank_path, customer_path) in enumerate(zip_longest(bank_paths, customer_paths), 1):
        bank, customer = audit_rows(bank_path), audit_rows(customer_path)
        if not (bank or customer):
            continue
        audit_id = (bank or customer)[0]["run_id"]
        start = bank[0]["payload"] if bank else {}
        session = start.get("conversation_id", fallback_session)
        fallback_session = session
        if audit_id not in seen:
            export_turn(client, experiment_id, run_id, {"task": task, "customer_mode": mode,
                        "audit_turn_id": audit_id}, customer, bank, session, start.get("turn_number", ordinal))
        count += 1
    return count
