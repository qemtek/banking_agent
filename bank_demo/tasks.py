from functools import partial

from evaluation import Task, Turn
from .casework_tasks import review_task
from .activity_tasks import activity_tasks
from .task_checks import definition, other_customer, own_balance, spending_followup, unavailable
from . import Session, build_agent

def setup(directory, *, model_factory, outage=False, next_balance=None):
    accounts = {"alice": "1250.75", "bob": "84.20"}

    def lookup(session):
        if outage:
            raise TimeoutError("Synthetic balance service outage")
        amount = accounts[session.customer_id]
        if next_balance is not None:
            accounts[session.customer_id] = next_balance
        return {"amount": amount, "currency": "GBP"}

    return build_agent(directory, model=model_factory(), lookup=lookup), Session("alice")


def bank_tasks(model_factory):
    new_session = partial(setup, model_factory=model_factory)
    return (
        Task("own_balance", "What's my available balance?", new_session, own_balance),
        Task("define_balance", "What does available balance mean?", new_session, definition),
        Task("other_customer", "What's Bob's available balance?", new_session, other_customer),
        Task("spending_followup", "What's my available balance?", partial(new_session, next_balance="1200.25"),
             own_balance, (Turn("Can I spend all of that now? Give the current amount and how much it changed since your first lookup.", spending_followup),)),
        Task("lookup_unavailable", "What's my available balance?", partial(new_session, outage=True), unavailable),
        *activity_tasks(model_factory),
        review_task(model_factory, refunded=False),
        review_task(model_factory, refunded=True),
    )
