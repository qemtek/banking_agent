import json
import os
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from uuid import uuid4

from .usage import capture_usage


class OperationFailed(Exception):
    """Signals an adapter failure after its audit event has been written."""


@contextmanager
def record_failure(audit, component, event_type, call_id=None, metadata=None):
    try:
        yield
    except Exception as error:
        audit.record(component, event_type, {"error": type(error).__name__, **(metadata or {})}, call_id)
        raise OperationFailed from error


def audited_model_call(method):
    @wraps(method)
    def wrapped(self, audit, messages, descriptions):
        audit.record("model", "model.requested", {"messages": messages, "tools": descriptions})
        with capture_usage() as metadata, record_failure(audit, "model", "model.failed", metadata=metadata):
            action = method(self, audit, messages, descriptions)
        audit.record("model", "model.responded", {type(action).__name__: asdict(action), **metadata})
        return action

    return wrapped


def audited_tool_call(method):
    @wraps(method)
    def wrapped(self, audit, action, session, call_id, tool, details=None):
        name = action.name
        audit.record("harness", "tool.started", {"name": name}, call_id)
        with record_failure(audit, name, "tool.failed", call_id):
            value = method(self, audit, action, session, call_id, tool, details)
        audit.record(name, "tool.completed", {"value": value}, call_id)
        return value

    return wrapped


def full_payload(event_type, payload):
    return payload


class LocalAudit:
    def __init__(self, directory: Path, select_payload=full_payload):
        self.directory = directory
        self.select_payload = select_payload

    def start(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return AuditRun(self.directory, self.select_payload)

    def resume(self, run_id):
        return AuditRun(self.directory, self.select_payload, run_id)


class AuditRun:
    def __init__(self, directory, select_payload, run_id=None):
        self.id = run_id or uuid4().hex
        self.sequence = 0
        self.select_payload = select_payload
        self.path = directory / (self.id + ".jsonl")
        if run_id is not None:
            with self.path.open("r+b") as source:
                offset = 0
                for number, line in enumerate(source, 1):
                    # A killed writer may leave only a prefix of its last JSON line.
                    if not line.endswith(b"\n"):
                        source.truncate(offset)
                        os.fsync(source.fileno())
                        break
                    offset += len(line)
                    event = json.loads(line)
                    if event["run_id"] != run_id or event["sequence"] != number:
                        raise ValueError("Invalid audit sequence")
                    self.sequence = number
            self.file = None
            return
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
        self.file = os.fdopen(fd, "w", encoding="utf-8")

    def record(self, component, event_type, payload, call_id=None):
        if self.file is None:
            self.file = os.fdopen(os.open(self.path, os.O_WRONLY | os.O_APPEND), "w", encoding="utf-8")
        event = {
            "run_id": self.id,
            "sequence": self.sequence + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "component": component,
            "event_type": event_type,
            "payload": self.select_payload(event_type, payload),
        }
        if call_id is not None:
            event["call_id"] = call_id
        self.file.write(json.dumps(event, allow_nan=False) + "\n")
        self.file.flush()
        os.fsync(self.file.fileno())
        self.sequence += 1

    def close(self):
        if self.file is not None:
            self.file.close()

    def park(self):
        self.close()
        self.file = None
