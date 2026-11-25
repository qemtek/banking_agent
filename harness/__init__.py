from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, Tuple, Union
from .checkpoints import CheckpointStore

from .audit import OperationFailed, audited_model_call, audited_tool_call
from .context import READ_RESULT, ContextBudget, ContextLimitExceeded, ContextMeasurementError, RunContext
from .middleware import RunMiddleware


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict


@dataclass(frozen=True)
class Reply:
    text: str


class WriteRejected(ValueError):
    """The adapter has established that this operation did not commit."""


class WriteAdapter(Protocol):
    def execute_once(self, session, operation_id: str, action: ToolCall, details: dict) -> dict: ...
    def reconcile(self, session, operation_id: str, action: ToolCall) -> Optional[dict]: ...


@dataclass(frozen=True)
class Tool:
    description: str
    execute: Callable[..., dict]
    summarize: Optional[Callable[[dict], dict]] = None
    parameters: Tuple[str, ...] = ()
    write: Optional[WriteAdapter] = None

    def specification(self):
        if not self.parameters:
            return self.description
        return {"description": self.description, "parameters": {
            "type": "object", "properties": {key: {"type": "string"} for key in self.parameters},
            "required": list(self.parameters), "additionalProperties": False}}

    def accepts(self, arguments):
        return (isinstance(arguments, dict) and arguments.keys() == set(self.parameters)
                and all(isinstance(value, str) for value in arguments.values()))


class Model(Protocol):
    def respond(self, messages: Sequence[dict], tools: Mapping[str, Union[str, dict]]) -> Union[Reply, ToolCall]:
        ...


@dataclass(frozen=True)
class Proposal:
    id: str
    action: ToolCall
    details: dict


@dataclass(frozen=True)
class Result:
    run_id: str
    reply: str
    stop_reason: str
    proposal: Optional[Proposal] = None


DEFAULT_MAX_TOOL_CALLS = 128


class Harness:
    def __init__(self, model: Model, instructions: str, tools: Mapping[str, Tool], audit, max_calls=DEFAULT_MAX_TOOL_CALLS, session_metadata=None, middleware=(),
                 context_budget: Optional[ContextBudget] = None, summarize_history=None,
                 checkpoint_store: Optional[CheckpointStore] = None, session_key=None):
        if type(max_calls) is not int or max_calls < 0:
            raise ValueError("max_calls must be a nonnegative integer")
        self.model = model
        self.instructions = instructions
        self.tools = dict(tools)
        if READ_RESULT in self.tools:
            raise ValueError(f"{READ_RESULT} is reserved for historical result retrieval")
        self.summarize_history = summarize_history or self._summarize_history
        self.audit = audit
        self.checkpoints = checkpoint_store
        self.session_key = session_key
        if checkpoint_store is not None and session_key is None:
            raise ValueError("A checkpoint store requires a trusted session key function")
        self.max_calls = max_calls
        self.session_metadata = session_metadata
        self.middleware = tuple(middleware)
        adapter_budget = getattr(model, "context_budget", None)
        if isinstance(adapter_budget, ContextBudget):
            if context_budget is not None and context_budget != adapter_budget:
                raise ValueError("Use the context budget configured by the model adapter")
            context_budget = adapter_budget
        self.context_budget = context_budget or ContextBudget()

    def conversation(self, session):
        return Conversation(self, session)

    def reopen(self, conversation_id, session):
        if self.checkpoints is None:
            raise ValueError("Configure a checkpoint store to reopen conversations")
        return Conversation(self, session, conversation_id)

    def run(self, message, session):
        conversation = self.conversation(session)
        result = conversation.send(message)
        if result.proposal is not None:
            return conversation._unavailable()
        return result

    def _decide(self, context, messages, tools, audit):
        ceiling = None
        while True:
            prepared, visible = context.prepare_request(messages, tools, audit, input_ceiling=ceiling)
            try:
                return self._ask_model(audit, prepared, visible.copy()), visible
            except OperationFailed as error:
                if not isinstance(error.__cause__, ContextLimitExceeded):
                    raise
                if ceiling is not None:
                    raise error.__cause__
                # Retry the decision only. Completed tools and the original conversation stay intact.
                ceiling = context.last_size * 9 // 10
                audit.record("context", "context.retry", {"input_limit": ceiling, "unit": context.budget.unit})

    @audited_model_call
    def _ask_model(self, audit, messages, descriptions):
        action = self.model.respond(messages, descriptions)
        if not isinstance(action, (Reply, ToolCall)):
            raise TypeError("Unsupported model response")
        return action

    @audited_tool_call
    def _execute_tool(self, audit, action, session, call_id, tool, details=None):
        if tool.write is not None:
            return tool.write.execute_once(session, call_id, action, details or {})
        return tool.execute(session, **action.arguments)

    def _summarize_history(self, messages):
        reply = self.model.respond(messages, {})
        if not isinstance(reply, Reply):
            raise ValueError("Expected a history summary")
        return reply.text


from .conversation import Conversation
