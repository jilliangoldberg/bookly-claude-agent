"""Application-only return confirmation. Model tool dispatch cannot import or call this path."""

from copy import deepcopy

from bookly.config import TODAY
from bookly.models import SessionState, tool_error, tool_ok
from bookly.policy import ineligible_reason, quote_amounts, resolution_allowed
from bookly.tools.orders import find_order, returned_item_ids


def _remember(session: SessionState, token: str, result: dict) -> dict:
    """Store one terminal action result so retries are idempotent."""
    session.action_results[token] = deepcopy(result)
    if session.pending_action and session.pending_action.token == token:
        session.pending_action = None
    return result


def confirm_return(session: SessionState, token: str) -> dict:
    """Revalidate and submit the exact pending proposal authorized by a UI control."""
    previous = session.action_results.get(token)
    if previous is not None:
        result = deepcopy(previous)
        if result.get("ok") and result.get("status") == "submitted":
            result["already_submitted"] = True
        return result

    pending = session.pending_action
    if pending is None or pending.token != token or pending.kind != "submit_return":
        return tool_error("ACTION_NOT_FOUND", "That return proposal is no longer available.")
    if session.verified_customer_id != pending.customer_id:
        return _remember(
            session,
            token,
            tool_error("ACTION_CUSTOMER_MISMATCH", "That return proposal does not belong to this session."),
        )

    quote = session.quotes.get(pending.quote_id)
    if quote is None:
        return _remember(session, token, tool_error("QUOTE_NOT_FOUND", "That return quote is no longer available."))

    order = find_order(session, quote.order_id)
    if order is None:
        return _remember(session, token, tool_error("ORDER_NOT_FOUND", "That order is no longer available."))

    items_by_id = {item["item_id"]: item for item in order["items"]}
    if any(item_id not in items_by_id for item_id in quote.item_ids):
        return _remember(
            session,
            token,
            tool_error("ACTION_STALE", "The books in this proposal have changed. Please prepare it again."),
        )

    returned = returned_item_ids(session, order)
    items = [items_by_id[item_id] for item_id in quote.item_ids]
    if any(ineligible_reason(order, item, TODAY, returned) is not None for item in items):
        return _remember(
            session,
            token,
            tool_error("ACTION_STALE", "This return is no longer eligible. Please review the order again."),
        )
    if not resolution_allowed(quote.reason, quote.resolution):
        return _remember(
            session,
            token,
            tool_error("ACTION_STALE", "That resolution is no longer available. Please prepare the return again."),
        )

    current_amounts = quote_amounts(items, quote.reason, quote.resolution)
    quoted_amounts = {"refund_amount": quote.refund_amount, "fee": quote.fee, "label": quote.label}
    if current_amounts != quoted_amounts:
        return _remember(
            session,
            token,
            tool_error("ACTION_STALE", "The return amount has changed. Please review a new quote."),
        )

    rma_number = f"RMA-{quote.order_id.removeprefix('BK-')}-{quote.quote_id.removeprefix('Q-')}"
    label = (
        "prepaid return label"
        if quote.label == "prepaid"
        else f"return label (${quote.fee:.2f} deducted from refund)"
    )
    return _remember(
        session,
        token,
        tool_ok(
            status="submitted",
            action_token=token,
            quote_id=quote.quote_id,
            order_id=quote.order_id,
            item_ids=list(quote.item_ids),
            item_titles=list(quote.item_titles),
            rma_number=rma_number,
            refund_amount=quote.refund_amount,
            resolution=quote.resolution,
            label_url=f"https://bookly.example.com/labels/{rma_number}.pdf",
            next_steps=(
                f"We've emailed a {label}. Pack the book(s) and drop them off within 14 days. "
                f"{quote.timing}"
            ),
        ),
    )


def cancel_return(session: SessionState, token: str) -> dict:
    """Cancel the matching proposal without changing any customer record."""
    previous = session.action_results.get(token)
    if previous is not None:
        return deepcopy(previous)
    pending = session.pending_action
    if pending is None or pending.token != token:
        return tool_error("ACTION_NOT_FOUND", "That return proposal is no longer available.")
    return _remember(
        session,
        token,
        tool_ok(status="cancelled", action_token=token, quote_id=pending.quote_id),
    )


def confirm_reply(result: dict) -> str:
    """The customer-facing message after Confirm, built only from the trusted action result."""
    if not result["ok"]:
        return result["message"]
    titles = ", ".join(result["item_titles"])
    return (
        f"Your return for **{titles}** has been submitted. "
        f"Your RMA is **{result['rma_number']}**. {result['next_steps']}"
    )


def cancel_reply(result: dict) -> str:
    """The customer-facing message after Cancel."""
    return "I've cancelled that return proposal. No return was submitted." if result["ok"] else result["message"]


def record_action_reply(session: SessionState, reply: str) -> None:
    """Add a code-written action reply to history, so the next model turn sees what the customer saw."""
    session.messages.append({"role": "assistant", "content": reply})


def invalidate_pending_action(session: SessionState, reason: str = "new_message") -> None:
    """Expire an old proposal when chat moves on; no customer-facing action is recorded."""
    pending = session.pending_action
    if pending is None:
        return
    _remember(
        session,
        pending.token,
        {
            "ok": False,
            "error_code": "ACTION_INVALIDATED",
            "message": "That return proposal is no longer available.",
            "status": "invalidated",
            "reason": reason,
            "action_token": pending.token,
            "quote_id": pending.quote_id,
        },
    )
