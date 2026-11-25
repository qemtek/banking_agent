from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import asdict
from threading import Lock
from uuid import uuid4

from . import Proposal, Reply, Result, Tool, ToolCall, WriteRejected
from .audit import OperationFailed
from .context import READ_RESULT, ContextLimitExceeded, ContextMeasurementError, RunContext
from .middleware import RunMiddleware


class Conversation:
    def __init__(self, harness, session, conversation_id=None):
        self.id = conversation_id or uuid4().hex
        self.harness = harness
        self.session = session
        self.store = harness.checkpoints
        self.owner = harness.session_key(session) if self.store is not None else None
        self._lock = Lock()
        self._audit = None
        self._context = RunContext(harness.context_budget, {n: t.summarize for n, t in harness.tools.items()},
                                   harness.summarize_history)
        self._state = {"history": [], "number": 0, "turn": None, "reviews": [], "pages": {}}
        if self.store is not None:
            if not isinstance(self.owner, str) or not self.owner:
                raise ValueError("A durable conversation requires a nonempty trusted session key")
            if conversation_id is None:
                self.store.create(self.id, self.owner, self._state)
            else:
                self._state = self.store.load(self.id, self.owner)

    @contextmanager
    def _exclusive(self):
        with self._lock, (self.store.lock(self.id) if self.store is not None else nullcontext()):
            if self.store is not None:
                self._state = self.store.load(self.id, self.owner)
                self._drain_audit()
                self._context.page_sources = {k: (src, bytes.fromhex(h)) for k, (src, h) in self._state["pages"].items()}
            try:
                if self._turn and self._audit is None:
                    self._audit = self.harness.audit.resume(self._turn["run_id"])
                yield
            finally:
                if self._audit is not None:
                    if not self._turn or self.store is not None:
                        self._audit.close()
                        self._audit = None
                    elif hasattr(self._audit, "park"):
                        self._audit.park()

    @property
    def _turn(self):
        return self._state["turn"]

    def _save(self):
        if self.store is not None:
            self._state["pages"] = {k: [src, h.hex()] for k, (src, h) in self._context.page_sources.items()}
            self.store.save(self.id, self.owner, self._state)

    def _transition(self, *events):
        if self.store is None:
            for kind, payload, call_id in events:
                self._audit.record("harness", kind, payload, call_id)
            return
        self._state["audit_pending"] = {
            "run": self._audit.id, "sequence": self._audit.sequence + 1, "events": events}
        self._save()
        self._drain_audit(self._audit)

    def _drain_audit(self, current=None):
        batch = self._state.get("audit_pending")
        if not batch:
            return
        audit = current or self.harness.audit.resume(batch["run"])
        try:
            for sequence, (kind, payload, call_id) in enumerate(batch["events"], batch["sequence"]):
                if audit.sequence < sequence:
                    if audit.sequence + 1 != sequence:
                        raise ValueError("Audit and checkpoint sequences disagree")
                    audit.record("harness", kind, payload, call_id)
            del self._state["audit_pending"]
            # Only the outbox changed. Page indexes have not yet been reloaded on restart.
            self.store.save(self.id, self.owner, self._state)
        finally:
            if current is None:
                audit.close()

    def _middleware(self):
        middleware = RunMiddleware(self.harness.middleware, self.session, self._audit)
        middleware.state = self._turn["middleware"]
        return middleware

    def _tools(self):
        return {**self.harness.tools, READ_RESULT: Tool(
            "Read a saved tool result by call_id, starting at character offset '0'. Continue with next_offset.",
            self._context.read_result, parameters=("call_id", "offset"))}

    def _finish(self, reply, reason, *events):
        turn = self._turn
        if turn["used"] or reason in ("final_reply", "tool_error", "tool_denied", "approval_declined",
                                     "approval_superseded", "recovered"):
            self._state["history"] = turn["messages"][1:] + [{"role": "assistant", "content": reply}]
        self._state["turn"] = None
        self._transition(*events, ("run.finished", {"reply": reply, "reason": reason}, None))
        self._audit.close()
        self._audit = None
        return Result(turn["run_id"], reply, reason)

    def send(self, message):
        with self._exclusive():
            if self._turn:
                operation = self._turn["operation"]
                if operation and operation["phase"] == "pending":
                    self._cancel("superseded", "customer")
                elif operation:
                    self._audit.record("harness", "operation.inquiry", {"message": message}, operation["id"])
                    result = self._recover()
                    if result is not None:
                        if self._turn is not None:
                            self._turn["messages"].extend([
                                {"role": "user", "content": message}, {"role": "assistant", "content": result.reply}])
                            self._save()
                        return result
                    self._finish("The earlier tool outcome has been recovered.", "recovered")
                else:
                    self._finish("The previous turn was interrupted.", "recovered")
            self._audit = self.harness.audit.start()
            self._state["number"] += 1
            messages = [{"role": "system", "content": self.harness.instructions}, *self._state["history"],
                        {"role": "user", "content": message}]
            self._state["turn"] = {"run_id": self._audit.id, "messages": messages, "used": 0,
                                  "middleware": {}, "operation": None}
            metadata = self.harness.session_metadata(self.session) if self.harness.session_metadata else {}
            payload = {"message": message, "session": metadata, "conversation_id": self.id,
                       "turn_number": self._state["number"], "max_tool_calls": self.harness.max_calls}
            for field, attr in (("model", "model_id"), ("reasoning_effort", "reasoning_effort")):
                value = getattr(self.harness.model, attr, None)
                if isinstance(value, str):
                    payload[field] = value
            self._audit.record("harness", "run.started", payload)
            self._save()
            return self._drive()

    def _proposal(self):
        op = self._turn["operation"]
        return Proposal(op["id"], ToolCall(**deepcopy(op["action"])), deepcopy(op["details"]))

    def pending_proposal(self):
        with self._exclusive():
            if not self._turn or not self._turn["operation"] or self._turn["operation"]["phase"] != "pending":
                return None
            proposal = self._proposal()
            denial, details, _ = self._access(proposal)
            if denial or details != proposal.details:
                raise PermissionError("Proposal unavailable for review")
            return proposal

    def _access(self, proposal):
        tools = self._tools()
        middleware = self._middleware()
        _, visible = middleware.before_model(self._turn["messages"], {n: t.specification() for n, t in tools.items()})
        name = proposal.action.name
        if name not in visible or not tools[name].accepts(proposal.action.arguments):
            return "This action is unavailable.", {}, False
        decision = middleware.before_tool(proposal.action, proposal.id)
        return decision.denial.reply if decision.denial else None, decision.review_details, decision.requires_approval

    def _decision_event(self, verdict, reviewer):
        op = self._turn["operation"]
        self._state["reviews"].append({"proposal_id": op["id"], "run_id": self._turn["run_id"],
                                      "decision": verdict, "reviewer": reviewer})

    def _cancel(self, verdict, reviewer):
        self._decision_event(verdict, reviewer)
        event = ("approval." + verdict, {"reviewer": reviewer}, self._turn["operation"]["id"])
        replies = {"superseded": "The proposed action was cancelled.", "declined": "The reviewer declined the proposed action.",
                   "unavailable": "This action needs an ongoing conversation for review."}
        return self._finish(replies[verdict], "approval_" + verdict, event)

    def _unavailable(self):
        with self._exclusive():
            return self._cancel("unavailable", "one-shot run")

    def review(self, proposal_id, *, approve, reviewer):
        with self._exclusive():
            op = self._turn and self._turn["operation"]
            if not op or op["phase"] != "pending" or op["id"] != proposal_id:
                raise ValueError("Proposal is no longer pending in this conversation")
            if type(approve) is not bool or not isinstance(reviewer, str) or not reviewer.strip():
                raise ValueError("Supply a boolean decision and a reviewer identity")
            proposal = self._proposal()
            try:
                denial, details, _ = self._access(proposal)
            except OperationFailed:
                return Result(self._audit.id, "I couldn't verify access to the proposed action.", "middleware_error")
            if denial or details != proposal.details:
                self._decision_event("invalidated", reviewer)
                event = ("approval.invalidated", {"reason": "access_or_details_changed"}, proposal.id)
                return self._finish(denial or "The action changed while awaiting review.",
                                    "tool_denied" if denial else "approval_superseded", event)
            if not approve:
                return self._cancel("declined", reviewer)
            self._decision_event("approved", reviewer)
            op["phase"] = "authorized"
            self._transition(("approval.approved", {"reviewer": reviewer}, proposal.id))
            outcome = self._dispatch(proposal.action, proposal.id, self._tools()[proposal.action.name], details)
            return outcome or self._drive()

    def _uncertain(self, reason):
        self._audit.record("harness", "operation.unconfirmed", {"reason": reason}, self._turn["operation"]["id"])
        return Result(self._audit.id, "The outcome is unconfirmed. I can check again later.", "operation_unconfirmed")

    def _recover(self):
        proposal = self._proposal()
        tool = self._tools().get(proposal.action.name)
        try:
            denial, details, requires_review = self._access(proposal)
        except OperationFailed:
            return self._uncertain("access_check_unavailable")
        if denial:
            self._audit.record("harness", "operation.access_denied", {}, proposal.id)
            return Result(self._audit.id, denial, "tool_denied")
        if tool is None or tool.write is None:
            return self._uncertain("adapter_unavailable")
        try:
            receipt = tool.write.reconcile(self.session, proposal.id, proposal.action)
        except Exception as error:
            return self._uncertain(type(error).__name__)
        self._audit.record("harness", "operation.reconciled", {"found": receipt is not None}, proposal.id)
        if receipt is not None:
            self._audit.record(proposal.action.name, "operation.receipt", {"value": receipt}, proposal.id)
            self._accept(proposal.action, proposal.id, receipt)
            return None
        reviewed = any(r["proposal_id"] == proposal.id and r["decision"] == "approved" for r in self._state["reviews"])
        if details != proposal.details or (requires_review and not reviewed):
            return self._finish("The earlier action was not committed and its review details have changed.", "approval_superseded")
        return self._dispatch(proposal.action, proposal.id, tool, details)

    def _accept(self, action, call_id, value):
        self._turn["messages"].extend([
            {"role": "assistant", "call_id": call_id, "tool_call": asdict(action)},
            {"role": "tool", "call_id": call_id, "name": action.name, "value": deepcopy(value)},
        ])
        self._turn["operation"] = None
        self._turn["used"] += 1
        self._save()

    def _dispatch(self, action, call_id, tool, details):
        try:
            value = self.harness._execute_tool(self._audit, action, self.session, call_id, tool, details)
        except OperationFailed as error:
            if isinstance(error.__cause__, WriteRejected):
                return self._finish("The requested action was not committed.", "tool_error")
            if tool.write is not None:
                return self._uncertain(type(error.__cause__).__name__)
            return self._finish("I couldn't retrieve the requested information.", "tool_error")
        if action.name == READ_RESULT:
            self._context.remember_page(call_id, action.arguments["call_id"])
        self._accept(action, call_id, value)
        return None

    def _drive(self):
        tools = self._tools()
        descriptions = {n: t.specification() for n, t in tools.items()}
        middleware = self._middleware()
        while True:
            try:
                messages, visible = middleware.before_model(self._turn["messages"], descriptions)
            except OperationFailed:
                return self._finish("I couldn't complete that request.", "middleware_error")
            try:
                action, visible = self.harness._decide(self._context, messages, visible, self._audit)
            except ContextLimitExceeded:
                return self._finish("I ran out of room for this request. Please narrow the question or send it in smaller parts.", "context_limit")
            except ContextMeasurementError:
                return self._finish("I couldn't prepare the request.", "context_error")
            except OperationFailed:
                return self._finish("I couldn't complete that request.", "model_error")
            if isinstance(action, Reply):
                return self._finish(action.text, "final_reply")
            call_id = uuid4().hex
            self._audit.record("harness", "tool.requested", asdict(action), call_id)
            reason = None
            if self._turn["used"] >= self.harness.max_calls:
                reason = "call_limit"
            elif action.name not in tools or not tools[action.name].accepts(action.arguments):
                reason = "invalid_tool_call"
            if reason:
                self._audit.record("harness", "tool.denied", {"reason": reason}, call_id)
                return self._finish("I couldn't complete that request.", reason)
            try:
                decision = middleware.before_tool(action, call_id)
            except OperationFailed:
                return self._finish("I couldn't complete that request.", "middleware_error")
            if decision.denial or action.name not in visible:
                self._audit.record("harness", "tool.denied", {"reason": decision.denial.reason if decision.denial else "tool_hidden"}, call_id)
                return self._finish(decision.denial.reply if decision.denial else "I couldn't complete that request.", "tool_denied")
            tool = tools[action.name]
            if self.store is not None and decision.requires_approval and tool.write is None:
                return self._finish("This reviewed action has no recovery adapter.", "tool_denied")
            if decision.requires_approval or tool.write is not None:
                self._turn["operation"] = {"phase": "pending" if decision.requires_approval else "authorized",
                                          "id": call_id, "action": asdict(action), "details": deepcopy(decision.review_details)}
                if not decision.requires_approval:
                    self._save()
            if decision.requires_approval:
                self._transition(("approval.requested", {**asdict(action), "details": decision.review_details}, call_id))
                return Result(self._audit.id, "This action is awaiting review.", "awaiting_approval", self._proposal())
            result = self._dispatch(action, call_id, tool, decision.review_details)
            if result is not None:
                return result
