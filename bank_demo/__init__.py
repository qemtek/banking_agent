import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from harness import Harness, Model, Tool
from harness.audit import LocalAudit
from harness.context import READ_RESULT
from harness.middleware import Denial, Middleware, ModelChanges, ToolDecision


@dataclass(frozen=True)
class Session:
    customer_id: str
    expires_at: Optional[datetime] = None
    login_id: Optional[str] = None


BANK_PROMPT_VERSION = 3
ESCALATION_POLICY = (Path(__file__).resolve().parents[1] / "docs/policies/escalation.md").read_text(encoding="utf-8")
POLICY_SHA256 = hashlib.sha256(ESCALATION_POLICY.encode()).hexdigest()


BALANCES = {"alice": "1250.75", "bob": "84.20"}


def available_balance(session):
    return {"amount": BALANCES[session.customer_id], "currency": "GBP"}


ACCOUNT_TOOLS = ("get_available_balance", "get_recent_transactions")


def recent_transactions(session):
    rows = {
        "alice": [{"reference": "TX-4102", "merchant": "Elm Books", "applied_at": "08:45",
                   "available_balance_effect": "-18.00"}],
        "bob": [{"reference": "TX-8701", "merchant": "Station Cafe", "applied_at": "08:50",
                 "available_balance_effect": "-4.80"}],
    }
    return {"currency": "GBP", "coverage_from": "08:00", "coverage_through": "09:05",
            "transactions": rows[session.customer_id]}


def balance_facts(value):
    return {"amount": value["amount"], "currency": value["currency"]}


class SessionAccess(Middleware):
    sign_in = "Your session has expired. Please sign in again to check your balance."

    def __init__(self, now, account_tools=ACCOUNT_TOOLS):
        self.account_tools = (*account_tools, READ_RESULT)
        self.now = now

    def expired(self, session):
        return session.expires_at is not None and self.now() >= session.expires_at

    def before_model(self, session, state, messages, tools):
        if not self.expired(session):
            return ModelChanges()
        return ModelChanges(
            state_updates={"session_expired": True},
            messages=[{"role": "system", "content": self.sign_in}],
            hide_tools=self.account_tools,
        )

    def before_tool(self, session, state, call):
        if call.name in self.account_tools and (state.get("session_expired") or self.expired(session)):
            return ToolDecision(
                state_updates={"session_expired": True},
                denial=Denial("session_expired", self.sign_in),
            )
        return ToolDecision()


def build_agent(audit_directory: Path, *, model: Model, now=lambda: datetime.now(timezone.utc), lookup=available_balance,
                transactions=recent_transactions, case_tools=None, middleware=(), checkpoint_store=None):
    tools = {
        "get_available_balance": Tool("Read the signed-in customer's available balance.", lookup, balance_facts),
        "get_recent_transactions": Tool("Read the signed-in customer's recent activity.", transactions),
    }
    tools.update(case_tools or {})
    return Harness(
        model=model,
        checkpoint_store=checkpoint_store,
        session_key=lambda s: json.dumps([s.customer_id, s.login_id]) if s.login_id else None,
        instructions=(
            "When asked about current balance or spending power, look up the customer's own balance "
            "and give the returned amount and currency. Explain terms directly. Refuse third-party balances.\n\n"
            + ESCALATION_POLICY
        ),
        tools=tools,
        audit=LocalAudit(audit_directory),
        middleware=[SessionAccess(now, tools), *middleware],
        session_metadata=lambda session: {"customer_id": session.customer_id},
    )
