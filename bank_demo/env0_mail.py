"""Serve the case's frozen merchant letter through the trial's env0 mailbox."""
import base64
import json
from dataclasses import replace
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from urllib.parse import quote
from urllib.request import Request, urlopen

MESSAGES = "http://127.0.0.1:9001/gmail/v1/users/me/messages"


def request(suffix="", payload=None):
    req = Request(MESSAGES + suffix, data=json.dumps(payload).encode() if payload else None,
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=10) as response:
        return json.load(response)


def mailbox_task(task):
    def setup(directory):
        scenario = task.setup(directory)
        tools = scenario.agent.tools
        original = tools["read_merchant_correspondence"]
        letter = original.execute(scenario.session)
        mail = EmailMessage()
        mail["From"] = "correspondence@merchant.example"
        mail["To"] = "card-reviews@bank.example"
        mail["Subject"] = letter["id"]
        mail.set_content(letter["body"])
        raw = base64.urlsafe_b64encode(mail.as_bytes()).decode()
        message_id = request(payload={"raw": raw, "labelIds": ["INBOX"]})["id"]

        def read(session):
            index = original.execute(session)
            if (index["id"], index["case_ref"]) != (letter["id"], letter["case_ref"]):
                raise ValueError("Correspondence unavailable")
            record = request("/" + quote(message_id, safe="") + "?format=raw")
            encoded = record["raw"]
            message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(encoded + "==="))
            if message["Subject"] != index["id"]:
                raise ValueError("Mailbox document identity differs from the case")
            body = message.get_body(preferencelist=("plain",))
            return {"id": index["id"], "case_ref": index["case_ref"], "body": body.get_content().rstrip("\r\n")}

        if read(scenario.session) != letter:
            raise ValueError("Mailbox seed differs from the frozen letter")
        tools["read_merchant_correspondence"] = replace(original, execute=read)
        return scenario
    return replace(task, setup=setup)
