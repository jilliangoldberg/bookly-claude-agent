"""Data shapes shared across the app. Only code writes to SessionState; the LLM can only
change it indirectly, by calling tools."""

from dataclasses import dataclass, field
from typing import Any, Literal


def tool_ok(**data) -> dict:
    """Build a successful tool result."""
    return {"ok": True, **data}


def tool_error(error_code: str, message: str, hint: str | None = None) -> dict:
    """Build a failed tool result. `message` is safe to paraphrase to the customer;
    `hint` tells the model what to do next and is never meant for the customer."""
    result = {"ok": False, "error_code": error_code, "message": message}
    if hint:
        result["hint"] = hint
    return result


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
class Quote:
    """A return Dewey has priced but not submitted. Amounts come from policy.py, never the LLM."""

    quote_id: str
    order_id: str
    item_ids: list[str]
    item_titles: list[str]
    reason: str  # changed_mind | damaged | wrong_item
    resolution: str  # refund_original | store_credit | replacement
    refund_amount: float
    fee: float
    label: str  # prepaid | fee_deducted
    timing: str
    created_turn: int


@dataclass(frozen=True)
class PendingAction:
    """An immutable proposal only the application UI can confirm."""

    token: str
    kind: Literal["submit_return"]
    customer_id: str
    quote_id: str
    created_turn: int


@dataclass
class SessionState:
    """Everything one conversation remembers. Nothing outlives the session (no PII at rest)."""

    turn_index: int = 0  # +1 per customer message; later used to prove a confirmation came on a new turn
    messages: list[Any] = field(default_factory=list)  # full API history, appended to, never edited
    verified_customer_id: str | None = None  # set only by verify_customer; order tools read identity from here
    verified_first_name: str | None = None  # so Dewey can say "Happy reading, Maya"
    verify_attempts: int = 0  # failed verification tries this session
    quotes: dict[str, Quote] = field(default_factory=dict)  # quote_id -> canonical proposal
    pending_action: PendingAction | None = None  # displayed and authorized outside the model loop
    action_results: dict[str, dict] = field(default_factory=dict)  # token -> idempotent confirm/cancel result
    escalation: dict | None = None  # the handoff ticket, once one exists; one per session
    trace: list[TraceEvent] = field(default_factory=list)
