import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Tuple, Union

from harness import Harness, Proposal, Result

from .scoring import Check, communication, scores


@dataclass(frozen=True)
class Turn:
    message: str
    score: Callable[[Result, Sequence[dict]], Sequence[Check]]


@dataclass(frozen=True)
class Scenario:
    agent: Harness
    session: Any
    before_turn: Callable[[int], None]
    score_state: Optional[Callable[[int], Sequence[Check]]] = None
    score_proposal: Optional[Callable[[int, Proposal], Sequence[Check]]] = None
    reviewer: Optional[Callable[[int, Proposal], Optional[bool]]] = None
    record_exchange: Optional[Callable[[Any, str, Result], None]] = None
    state_database: Optional[Path] = None


@dataclass(frozen=True)
class Task:
    name: str
    message: str
    setup: Callable[[Path], Union[Tuple[Harness, Any], Scenario]]
    score: Callable[[Result, Sequence[dict]], Sequence[Check]]
    followups: Sequence[Turn] = ()
    render_reply: Optional[Callable[[Result, Sequence[dict]], Result]] = None
    score_history: Optional[Callable[[Result, Sequence[Sequence[dict]]], Sequence[Check]]] = None

    def score_turn(self, turn, result, events, history):
        checks = list(turn.score(result, events))
        if self.score_history:
            history.append(events)
            checks.extend(replace(c, source="history") for c in self.score_history(result, history))
        return checks


@dataclass(frozen=True)
class Outcome:
    task: str
    status: str
    audit: str = ""
    checks: Sequence[Check] = ()
    error: str = ""
    turn_audits: Sequence[str] = ()
    dialogue: Sequence[dict] = ()
    dimensions: dict = field(default_factory=dict)


def read_turn(path, result, conversation_id, number):
    with path.open(encoding="utf-8") as source:
        events = [json.loads(line) for line in source]
    terminal = "approval.requested" if result.stop_reason == "awaiting_approval" else "run.finished"
    if not events or events[0]["event_type"] != "run.started" or events[-1]["event_type"] != terminal:
        raise ValueError("Incomplete audit")
    start = events[0]["payload"]
    if start["conversation_id"] != conversation_id or start["turn_number"] != number:
        raise ValueError("Conversation or turn mismatch")
    for sequence, event in enumerate(events, 1):
        if event["sequence"] != sequence or event["run_id"] != result.run_id:
            raise ValueError("Audit sequence or run ID mismatch")
    return events


def copy_database(source, target):
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        with closing(sqlite3.connect(target)) as snapshot:
            db.backup(snapshot)


def evaluate(task: Task, directory: Path, *, customer=None) -> Outcome:
    paths = []
    checks = []
    dialogue = []
    exchanges = []
    history = []

    def outcome(error=""):
        overall, dimensions = scores(checks, error=bool(error))
        return Outcome(task.name, overall, paths[-1] if paths else "", checks, error, paths, dialogue, dimensions)

    try:
        fixture = task.setup(directory)
        if isinstance(fixture, Scenario):
            agent, session = fixture.agent, fixture.session
        else:
            agent, session = fixture
        conversation = agent.conversation(session)
        turns = (Turn(task.message, task.score), *task.followups)
        deferred = None
        for number, turn in enumerate(turns, 1):
            if isinstance(fixture, Scenario):
                fixture.before_turn(number)
            message = turn.message
            if customer is not None:
                try:
                    message = customer.speak(tuple(exchanges))
                except Exception as error:
                    return outcome(f"Customer turn {number}: {type(error).__name__}: {error}")
            dialogue.append({"speaker": "customer", "text": message})
            result = conversation.send(message)
            path = directory / (result.run_id + ".jsonl")
            paths.append(str(path))
            if deferred is not None:
                try:
                    conversation.review(deferred, approve=True, reviewer="fixture")
                except ValueError:
                    checks.append(Check("Correction invalidated the earlier approval", True, deferred, "proposal", source="review"))
                else:
                    raise RuntimeError("Stale approval accepted")
                deferred = None
            while result.proposal is not None:
                if not isinstance(fixture, Scenario) or not fixture.score_proposal or not fixture.reviewer:
                    raise ValueError("Scenario has no reviewer for a pending proposal")
                proposal = result.proposal
                scored = list(fixture.score_proposal(number, proposal))
                if not scored:
                    raise ValueError("Missing pre-review evidence")
                checks.extend(replace(c, criterion=f"Turn {number} · {c.criterion}", source="review") for c in scored)
                decision = fixture.reviewer(number, proposal)
                if decision is None:
                    if number == len(turns):
                        raise ValueError("Deferred proposal requires a following customer turn")
                    deferred = proposal.id
                    break
                result = conversation.review(proposal.id, approve=decision, reviewer="fixture")
            events = read_turn(path, result, conversation.id, number)
            if task.render_reply:
                result = task.render_reply(result, events)
            dialogue.append({"speaker": "bank", "text": result.reply})
            exchanges.append((message, result.reply))
            if isinstance(fixture, Scenario) and fixture.record_exchange:
                fixture.record_exchange(session, message, result)
            if isinstance(fixture, Scenario) and fixture.state_database:
                snapshots = directory / "snapshots"
                snapshots.mkdir(exist_ok=True)
                copy_database(fixture.state_database, snapshots / (result.run_id + ".sqlite"))
            state_checks = list(fixture.score_state(number)) if isinstance(fixture, Scenario) and fixture.score_state else []
            judgments = [*task.score_turn(turn, result, events, history), *(replace(c, source="state") for c in state_checks)]
            if task.followups:
                judgments = [replace(c, criterion=f"Turn {number} · {c.criterion}") for c in judgments]
            checks.extend(judgments)
            if result.stop_reason in ("model_error", "context_error", "middleware_error"):
                failures = [e for e in events if e["event_type"].endswith(".failed")]
                detail = failures[-1]["payload"].get("error") if failures else result.stop_reason
                return outcome(f"Bank turn {number}: {detail}")
            if not judgments:
                raise ValueError("Turn supplied no criteria")
        return outcome()
    except Exception as error:
        return outcome(f"{type(error).__name__}: {error}")
