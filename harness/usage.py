from contextlib import contextmanager
from contextvars import ContextVar


_active_usage = ContextVar("model_usage", default=None)


@contextmanager
def capture_usage():
    # A decision can fail after the provider has already returned its usage.
    metadata = {}
    token = _active_usage.set(metadata)
    try:
        yield metadata
    finally:
        _active_usage.reset(token)


def record_usage(data):
    target = _active_usage.get()
    if target is None or not isinstance(data, dict):
        return
    counts = {}
    for key in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens"):
        value = data.get(key)
        if type(value) is int and value >= 0:
            counts[key] = value
    if "input_tokens" not in counts or "output_tokens" not in counts:
        return
    counts["total_tokens"] = counts["input_tokens"] + counts["output_tokens"]
    target["usage"] = counts
