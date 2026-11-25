import argparse
from datetime import datetime, timezone
from pathlib import Path

from bank_demo import Session, build_agent
from adapters.codex_cli import model_factory


def main():
    parser = argparse.ArgumentParser(description="Run the bank demo with Terra at medium reasoning effort.")
    parser.add_argument("--chat", action="store_true", help="Start a conversation as synthetic customer Alice")
    parser.add_argument("--require-verified-context", action="store_true",
                        help="Require provider token verification and an enforced generation cap")
    args = parser.parse_args()
    directory = Path("audits")
    try:
        create_model = model_factory(require_verified_context=args.require_verified_context)
    except ValueError as error:
        parser.error(str(error))
    agent = build_agent(directory, model=create_model())
    if args.chat:
        conversation = agent.conversation(Session("alice"))
        print("Signed in as Alice. Enter /quit to end this session.")
        while True:
            try:
                message = input("You> ").strip()
            except EOFError:
                return
            if message == "/quit":
                return
            if message:
                print(conversation.send(message).reply)
    sessions = [Session("alice"), Session("bob"), Session("alice", expires_at=datetime.now(timezone.utc))]
    for session in sessions:
        result = agent.run("What's my available balance?", session)
        print(f"{session.customer_id}: {result.reply}  ({directory / (result.run_id + '.jsonl')})")


if __name__ == "__main__":
    main()
