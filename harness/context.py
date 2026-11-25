import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence, Union

from .history import TurnHistory, digest


READ_RESULT = "read_tool_result"


def request_bytes(messages, tools):
    body = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)
    return len(body.encode("utf-8"))


@dataclass(frozen=True)
class ContextBudget:
    window: int = 16384
    reserve: int = 1024
    measure: Callable[[Sequence[dict], Mapping[str, Union[str, dict]]], int] = request_bytes
    unit: str = "bytes"

    def __post_init__(self):
        if type(self.window) is not int or type(self.reserve) is not int:
            raise TypeError("window and reserve must be integers")
        if not 0 <= self.reserve < self.window:
            raise ValueError("reserve must be nonnegative and smaller than the context window")

    def _size(self, messages, tools):
        size = self.measure(messages, tools)
        return self.validate_size(size)

    def validate_size(self, size):
        if type(size) is not int or size < 0:
            raise ValueError("measure must return a nonnegative integer")
        return size

    def require_fit(self, size):
        if self.validate_size(size) > self.window - self.reserve:
            raise ContextLimitExceeded

    def prepare(self, messages, tools, audit):
        return RunContext(self, {}).prepare(messages, tools, audit)


class RunContext:
    def __init__(self, budget, summarizers, summarize_history=None):
        self.budget = budget
        self.summarizers = summarizers
        self.history = TurnHistory(summarize_history)
        self.cache = {}
        self.readable = {}
        self.page_sources = {}
        self.last_size = 0

    def read_result(self, session, call_id, offset):
        source = self.readable.get(call_id)
        if source is None:
            return {"error": "Result unavailable"}
        if not offset.isascii() or not offset.isdecimal() or len(offset) > 16:
            return {"error": "Use a nonnegative character offset"}
        data = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        start = int(offset)
        if start > len(data):
            return {"error": "Offset exceeds result length"}
        page_chars = min(8192, max(512, (self.budget.window - self.budget.reserve) // 8))
        end = min(start + page_chars, len(data))
        return {"text": data[start:end], "next_offset": str(end) if end < len(data) else None}

    def remember_page(self, page_id, source_id):
        if source_id in self.readable:
            self.page_sources[page_id] = (source_id, digest(self.readable[source_id]))

    def prepare(self, messages, tools, audit):
        return self.prepare_request(messages, tools, audit)[0]

    def prepare_request(self, messages, tools, audit, input_ceiling=None):
        limit = self.budget.window - self.budget.reserve
        if input_ceiling is not None:
            limit = min(limit, input_ceiling)
        descriptions = dict(tools)
        reader = descriptions.pop(READ_RESULT, None)
        messages = list(messages)
        pairs = list(_tool_pairs(messages))
        self.readable = {}
        try:
            sources = self._visible_sources(messages, pairs, descriptions, reader)
            initial = self.budget._size(messages, descriptions)
            prepared, descriptions, size, applied, created = self._mask_results(
                messages, pairs, descriptions, reader, sources, initial, limit)
            # Summarized turns may also contain source IDs for calls that were too small to mask.
            if size > limit or self.history.checkpoint:
                history_tools = dict(descriptions)
                if sources:
                    history_tools[READ_RESULT] = reader
                revised = self.history.compact(prepared, history_tools, self.budget, limit, audit)
                if revised is not prepared:
                    revised_size = self.budget._size(revised, history_tools)
                    if revised_size < size:
                        prepared, descriptions, size = revised, history_tools, revised_size
            if size > limit:
                prepared, size = self._fit_read_page(prepared, descriptions, size, limit)
            if READ_RESULT in descriptions:
                self.readable = sources
        except ContextLimitExceeded:
            audit.record("context", "context.rejected", {"stage": "preparation"})
            raise
        except Exception as error:
            audit.record("context", "context.failed", {"error": type(error).__name__})
            raise ContextMeasurementError from error
        if created:
            audit.record("context", "context.compacted", {"summaries": created})
        audit.record("context", "context.checked", {
            "unit": self.budget.unit, "window": self.budget.window, "reserve": self.budget.reserve,
            "input_limit": limit, "original_size": initial, "final_size": size,
            "summarized_call_ids": applied, "fits": size <= limit,
        })
        if size > limit:
            raise ContextLimitExceeded
        self.last_size = size
        return prepared, descriptions

    def _fit_read_page(self, messages, tools, size, limit):
        if len(messages) < 2:
            return messages, size
        request, result = messages[-2:]
        if result.get("role") != "tool" or result.get("name") != READ_RESULT:
            return messages, size
        call = request.get("tool_call", {})
        if call.get("name") != READ_RESULT or request.get("call_id") != result.get("call_id"):
            return messages, size
        page = result["value"]
        text = page.get("text") if isinstance(page, dict) else None
        if not isinstance(text, str):
            return messages, size
        offset = int(call["arguments"]["offset"])
        length = len(text) // 2
        while length:
            shortened = {**page, "text": text[:length], "next_offset": str(offset + length)}
            candidate = [*messages[:-1], {**result, "value": shortened}]
            new_size = self.budget._size(candidate, tools)
            if new_size <= limit:
                return candidate, new_size
            length //= 2
        return messages, size

    def _visible_sources(self, messages, pairs, descriptions, reader):
        sources = {call_id: messages[index + 1]["value"] for index, call_id in pairs
                   if reader and messages[index + 1]["name"] in descriptions}
        for index, message in enumerate(messages):
            if message.get("role") == "tool" and message.get("name") == READ_RESULT:
                source_id, fingerprint = self.page_sources.get(message.get("call_id"), (None, None))
                if source_id not in sources or fingerprint != digest(sources[source_id]):
                    messages[index] = {**message, "value": {"error": "Result unavailable"}}
        return sources

    def _mask_results(self, messages, pairs, descriptions, reader, sources, initial, limit):
        applied = []
        created = []
        candidate = messages.copy()

        def remaining():
            return [message for message in candidate if message is not None]

        size = initial
        dispatch_size = initial
        active = descriptions
        for index, call_id in pairs:
            if (index, call_id) == pairs[-1]:
                # A fresh result may itself be larger than the model's entire window.
                if dispatch_size <= limit or call_id not in sources:
                    break
            request, result = messages[index:index + 2]
            name = result["name"]
            cached = self.cache.get(call_id)
            if not cached and dispatch_size * 10 <= limit * 9:
                continue
            summarize = self.summarizers.get(name)
            if not summarize and call_id not in sources and name != READ_RESULT:
                continue
            fingerprint = digest([request, result])
            reuse = cached and cached[0] == fingerprint
            if reuse:
                summary = deepcopy(cached[1])
            else:
                facts = summarize(deepcopy(result["value"])) if summarize else {}
                if not isinstance(facts, dict):
                    raise TypeError("A tool summary must contain a dictionary of facts")
                summary = {"role": "tool_summary", "name": name, "call_id": call_id, "facts": facts}
                if request["tool_call"]["arguments"]:
                    summary["arguments"] = deepcopy(request["tool_call"]["arguments"])
            candidate[index:index + 2] = [summary, None]
            new_size = self.budget._size(remaining(), descriptions)
            if new_size >= size:
                candidate[index:index + 2] = [request, result]
                continue
            if not reuse:
                self.cache[call_id] = (fingerprint, deepcopy(summary))
                created.append(summary)
            size = new_size
            applied.append(call_id)
            if active is descriptions and call_id in sources:
                active = {**descriptions, READ_RESULT: reader}
            dispatch_size = self.budget._size(remaining(), active) if active is not descriptions else size

        if dispatch_size > initial:
            return messages, descriptions, initial, [], []
        return remaining(), active, dispatch_size, applied, created


def _tool_pairs(messages):
    for index, (request, result) in enumerate(zip(messages, messages[1:])):
        call_id = request.get("call_id")
        if (request.get("role") == "assistant" and "tool_call" in request
                and request["tool_call"].get("name") == result.get("name")
                and isinstance(request["tool_call"].get("arguments"), dict)
                and result.get("role") == "tool" and call_id and result.get("call_id") == call_id):
            yield index, call_id


class ContextLimitExceeded(Exception):
    pass


class ContextMeasurementError(Exception):
    pass
