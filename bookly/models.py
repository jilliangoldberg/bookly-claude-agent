"""Data shapes shared across the app. Only code writes to SessionState; the LLM can only
change it indirectly, by calling tools."""

from dataclasses import dataclass, field
from typing import Any, Literal


def tool_ok(**data) -> dict:
    """Build a successful tool result."""
    return {"ok": True, **data}


def tool_error(error_code: str, message: str) -> dict:
    """Build a failed tool result. The message must be safe for Dewey to paraphrase to the customer."""
    return {"ok": False, "error_code": error_code, "message": message}


@dataclass
class TraceEvent:
    """One step the agent took, shown in the UI's 'glass box' trace."""

    turn: int  # which customer message this step belongs to
    kind: Literal["llm", "tool"]
    name: str  # model name for LLM steps, tool name for tool calls
    input: dict
    output: dict
    ok: bool
    latency_ms: int


@dataclass
class SessionState:
    """Everything one conversation remembers. Nothing outlives the session (no PII at rest)."""

    turn_index: int = 0  # +1 per customer message; later used to prove a confirmation came on a new turn
    messages: list[Any] = field(default_factory=list)  # full API history, appended to, never edited
    verified_customer_id: str | None = None  # set only by verify_customer; order tools read identity from here
    verified_first_name: str | None = None  # so Dewey can say "Happy reading, Maya"
    verify_attempts: int = 0  # failed verification tries this session
    trace: list[TraceEvent] = field(default_factory=list)
