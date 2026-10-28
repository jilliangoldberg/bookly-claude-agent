"""A thin proxy around the Anthropic client that records every call's served model, tokens, and
latency, and refuses to score a call that a different model answered."""

import time
from dataclasses import asdict, dataclass

from evals import PRICES


class ModelMismatch(RuntimeError):
    """The API served a different model than the one requested; the trial can't be scored."""


@dataclass
class CallRecord:
    """One API call, for cost and latency accounting."""

    role: str  # "agent", "customer", or "judge"
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    latency_ms: int

    @property
    def cost(self) -> float:
        """USD for this call, from recorded tokens and the model's price row."""
        p = PRICES.get(self.model)
        if p is None:
            return 0.0
        return (self.input_tokens * p["input"] + self.output_tokens * p["output"]
                + self.cache_read_tokens * p["cache_read"] + self.cache_write_tokens * p["cache_write"]) / 1_000_000

    def to_dict(self) -> dict:
        """JSON-friendly form, cost included."""
        return {**asdict(self), "cost": round(self.cost, 6)}


class RecordingClient:
    """Pass as `api=` to handle_user_message (or use directly): forwards messages.create / .parse
    to the real client and appends a CallRecord to `log` for each call."""

    def __init__(self, inner, role: str, log: list[CallRecord]):
        self.inner = inner
        self.role = role
        self.log = log
        self.messages = self  # so callers can write client.messages.create(...)

    def create(self, **request):
        return self._call(self.inner.messages.create, request)

    def parse(self, **request):
        return self._call(self.inner.messages.parse, request)

    def _call(self, method, request: dict):
        started = time.monotonic()
        response = method(**request)
        usage = response.usage
        self.log.append(CallRecord(
            role=self.role,
            model=response.model,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            latency_ms=int((time.monotonic() - started) * 1000),
        ))
        if response.model != request["model"]:
            raise ModelMismatch(f"{self.role}: asked for {request['model']}, served by {response.model}")
        return response
