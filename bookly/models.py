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

    # Which customer message this step belongs to
    turn: int
    kind: Literal["llm", "tool", "validation"]
    # Model name, tool name, or "check_reply"
    name: str
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
    # changed_mind | damaged | wrong_item
    reason: str
    # refund_original | store_credit | replacement
    resolution: str
    refund_amount: float
    fee: float
    # prepaid | fee_deducted
    label: str
    timing: str
    created_turn: int


# Frozen: a proposal can't be edited after it's shown
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

    # +1 per customer message; proves a confirmation came on a new turn
    turn_index: int = 0
    # Full API history; append-only
    messages: list[Any] = field(default_factory=list)
    # Set only by verify_customer; order tools read identity from here
    verified_customer_id: str | None = None
    # For "Happy reading, Maya"
    verified_first_name: str | None = None
    # Failed verification tries this session
    verify_attempts: int = 0
    # quote_id -> canonical proposal
    quotes: dict[str, Quote] = field(default_factory=dict)
    # Shown and authorized outside the model loop
    pending_action: PendingAction | None = None
    # token -> confirm/cancel result; makes retries idempotent
    action_results: dict[str, dict] = field(default_factory=dict)
    # Handoff ticket, once one exists; one per session
    escalation: dict | None = None
    # Every LLM call, tool call, and reply check; the sidebar shows it
    trace: list[TraceEvent] = field(default_factory=list)
