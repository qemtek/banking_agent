from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional, Sequence, Tuple

from .audit import record_failure


@dataclass(frozen=True)
class ModelChanges:
    """State updates survive the turn. Message replacements and tool filters last one model call."""
    state_updates: dict = field(default_factory=dict)
    messages: Optional[Sequence[dict]] = None
    hide_tools: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Denial:
    reason: str
    reply: str


@dataclass(frozen=True)
class ToolDecision:
    state_updates: dict = field(default_factory=dict)
    denial: Optional[Denial] = None
    requires_approval: bool = False
    review_details: dict = field(default_factory=dict)


class Middleware:
    def before_model(self, session: Any, state: Mapping, messages: Sequence[dict], tools: Mapping):
        return ModelChanges()

    def before_tool(self, session: Any, state: Mapping, call):
        return ToolDecision()


class RunMiddleware:
    def __init__(self, middleware, session, audit):
        self.middleware = middleware
        self.session = session
        self.audit = audit
        self.state = {}

    def _invoke(self, hook, expected, args, call_id=None):
        component = type(hook.__self__).__qualname__
        metadata = {"hook": hook.__name__}
        self.audit.record(component, "middleware.started", metadata, call_id)
        with record_failure(self.audit, component, "middleware.failed", call_id):
            change = hook(deepcopy(self.session), deepcopy(self.state), *deepcopy(args))
            if not isinstance(change, expected):
                raise TypeError(f"{hook.__name__} must return {expected.__name__}")
            updates = deepcopy(change.state_updates)
            if not isinstance(updates, dict):
                raise TypeError("state_updates must be a dictionary")
            payload = asdict(change)
        self.audit.record(component, "middleware.decided", {**metadata, **payload}, call_id)
        self.state.update(updates)
        return change

    def before_model(self, messages, tools):
        for layer in self.middleware:
            change = self._invoke(layer.before_model, ModelChanges, (messages, tools))
            if change.messages is not None:
                messages = change.messages
            tools = {name: text for name, text in tools.items() if name not in change.hide_tools}
        return deepcopy(messages), dict(tools)

    def before_tool(self, call, call_id):
        approval = False
        details = {}
        for index, layer in enumerate(self.middleware):
            change = self._invoke(layer.before_tool, ToolDecision, (call,), call_id)
            if change.denial is not None:
                return change
            approval = approval or change.requires_approval
            if change.requires_approval and change.review_details:
                details[f"{index}:{type(layer).__qualname__}"] = deepcopy(change.review_details)
        return ToolDecision(requires_approval=approval, review_details=details)
