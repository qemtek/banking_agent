import http.client
import json
import os

from harness import ContextBudget, Reply, ToolCall
from harness.context import ContextLimitExceeded
from harness.usage import record_usage


class ProviderError(RuntimeError):
    def __init__(self, status, request_id):
        super().__init__(f"OpenAI HTTP {status} (request {request_id})")
        self.status = status
        self.request_id = request_id


def _tool_history(name, call_id, arguments, value):
    return [
        {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(arguments)},
        {"type": "function_call_output", "call_id": call_id, "output": json.dumps(value)},
    ]


class OpenAIResponses:
    model_id = "gpt-5.4-mini-2026-03-17"

    def __init__(self, api_key):
        if not api_key:
            raise ValueError("Set OPENAI_API_KEY in the environment before starting the agent.")
        self._api_key = api_key
        self.context_budget = ContextBudget(window=400_000, reserve=16_384, measure=self.count_input, unit="tokens")

    def _post(self, path, body):
        connection = http.client.HTTPSConnection("api.openai.com", timeout=600)
        try:
            connection.request("POST", path, body=json.dumps(body).encode(), headers={
                "Authorization": "Bearer " + self._api_key, "Content-Type": "application/json",
            })
            response = connection.getresponse()
            if response.status != 200:
                if response.status in (400, 413):
                    try:
                        error = json.load(response).get("error")
                    except (ValueError, AttributeError):
                        error = None
                    if isinstance(error, dict) and error.get("code") == "context_length_exceeded":
                        raise ContextLimitExceeded("Provider rejected the input context")
                raise ProviderError(response.status, response.getheader("x-request-id", "unknown"))
            return json.load(response)
        finally:
            connection.close()

    def _request(self, messages, tools):
        items = []
        for message in messages:
            role = message["role"]
            if role == "assistant" and "tool_call" in message:
                call = message["tool_call"]
                items.append({"type": "function_call", "call_id": message["call_id"],
                              "name": call["name"], "arguments": json.dumps(call["arguments"])})
            elif role == "tool":
                items.append({"type": "function_call_output", "call_id": message["call_id"],
                              "output": json.dumps(message["value"])})
            elif role == "tool_summary":
                items.extend(_tool_history(message["name"], message["call_id"], message.get("arguments", {}),
                                           {"retained_facts": message["facts"]}))
            elif role in ("system", "developer", "user", "assistant"):
                items.append({"role": role, "content": message["content"]})
            else:
                raise ValueError(f"Unsupported message role: {role}")
        functions = []
        for name, spec in tools.items():
            if isinstance(spec, str):
                spec = {"description": spec, "parameters": {"type": "object", "properties": {},
                                                            "required": [], "additionalProperties": False}}
            functions.append({"type": "function", "name": name, "strict": True, **spec})
        body = {"model": self.model_id, "input": items}
        if functions:
            body.update(tools=functions, parallel_tool_calls=False)
        return body

    def count_input(self, messages, tools):
        return self._post("/v1/responses/input_tokens", self._request(messages, tools))["input_tokens"]

    def respond(self, messages, tools):
        body = self._request(messages, tools)
        count = self._post("/v1/responses/input_tokens", body)["input_tokens"]
        self.context_budget.require_fit(count)
        body.update(store=False, max_output_tokens=min(128_000, self.context_budget.window - count))
        response = self._post("/v1/responses", body)
        usage = response.get("usage")
        if isinstance(usage, dict):
            inputs = usage.get("input_tokens_details")
            outputs = usage.get("output_tokens_details")
            record_usage({**usage, "cached_input_tokens": inputs.get("cached_tokens") if isinstance(inputs, dict) else None,
                          "reasoning_output_tokens": outputs.get("reasoning_tokens") if isinstance(outputs, dict) else None})
        if response.get("status") == "incomplete" and (response.get("incomplete_details") or {}).get("reason") == "max_output_tokens":
            raise ValueError("Generation exhausted its output token allowance")
        if response.get("status") != "completed":
            raise ValueError("OpenAI response did not complete")
        calls = []
        text = []
        for item in response["output"]:
            if item["type"] == "function_call":
                calls.append(item)
            elif item["type"] == "message":
                for part in item["content"]:
                    if part["type"] == "output_text":
                        text.append(part["text"])
                    elif part["type"] == "refusal":
                        text.append(part["refusal"])
        if len(calls) == 1:
            call = calls[0]
            arguments = json.loads(call["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be an object")
            return ToolCall(call["name"], arguments)
        if calls or not any(text):
            raise ValueError("Expected a single function call or a text reply")
        return Reply("\n".join(text))


def create_model():
    return OpenAIResponses(os.environ.get("OPENAI_API_KEY"))
