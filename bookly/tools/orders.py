"""Order tools: read-only lookups, scoped to the verified customer. The customer ID always
comes from the session, never from tool input, so the LLM can't ask for someone else's orders."""

import re

from bookly import data
from bookly.config import TODAY
from bookly.models import SessionState, tool_error, tool_ok
from bookly.policy import INELIGIBLE_EXPLANATIONS, ineligible_reason

NOT_VERIFIED = tool_error(
    "NOT_VERIFIED",
    "I need to verify your account before I can look at orders.",
    hint="Ask for the account email and ZIP code together, then call verify_customer.",
)


def normalize_order_id(raw: str) -> str:
    """Turn what a customer typed ('1005', '#bk-1005', 'BK 1005') into the canonical 'BK-1005'."""
    digits = re.sub(r"\D", "", raw)
    return f"BK-{digits}" if digits else raw.strip().upper()


def order_not_found(order_id: str) -> dict:
    """The one 'not found' answer, used whether the order is someone else's or doesn't exist."""
    return tool_error(
        "NOT_FOUND",
        f"I can't find order {normalize_order_id(order_id)} on this account.",
        hint="Don't suggest the order belongs to someone else. Offer to look at the customer's own orders.",
    )

LIST_ORDERS_SCHEMA = {
    "name": "list_orders",
    "description": (
        "List the verified customer's orders as short summaries (id, date, type, status, titles). "
        "Use it to find which order the customer means. Requires verification first."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
}

GET_ORDER_SCHEMA = {
    "name": "get_order",
    "description": (
        "Get full details for one of the verified customer's orders: items, each shipment's status, "
        "carrier, tracking number, ETA or delivered date, preorder release date, and whether each item "
        "can be returned (with the reason if not). Use it before reporting any order status or starting a return."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "Order id like BK-1001. Pass what the customer typed; code normalizes it."}
        },
        "required": ["order_id"],
        "additionalProperties": False,
    },
}


def overall_status(order: dict) -> str:
    """Summarize an order's state in a few words, e.g. 'partially delivered'."""
    if order["order_type"] == "preorder":
        return f"preorder, releases {order['release_date']}"
    if order["order_type"] == "digital":
        return "digital, available in library"
    statuses = {s["status"] for s in order["shipments"]}
    if statuses == {"delivered"}:
        return "delivered"
    if "delivered" in statuses:
        return "partially delivered"
    return "in transit" if "in_transit" in statuses else "processing"


def list_orders(session: SessionState) -> dict:
    """Return the verified customer's orders as short summaries."""
    if session.verified_customer_id is None:
        return NOT_VERIFIED

    summaries = [
        {
            "order_id": o["order_id"],
            "placed_date": o["placed_date"],
            "order_type": o["order_type"],
            "status": overall_status(o),
            "titles": [item["title"] for item in o["items"]],
        }
        for o in data.orders()
        if o["customer_id"] == session.verified_customer_id
    ]
    return tool_ok(orders=summaries)


def find_order(session: SessionState, order_id: str) -> dict | None:
    """Look up an order only if it belongs to the verified customer; otherwise None."""
    wanted = normalize_order_id(order_id)
    return next(
        (o for o in data.orders() if o["order_id"] == wanted and o["customer_id"] == session.verified_customer_id),
        None,
    )


def returned_item_ids(session: SessionState, order: dict) -> set[str]:
    """Items with a return already on file or submitted earlier in this chat."""
    on_file = {item_id for r in order["returns"] for item_id in r["item_ids"]}
    this_chat = {
        item_id
        for result in session.action_results.values()
        if result.get("ok") and result.get("order_id") == order["order_id"]
        for item_id in result.get("item_ids", [])
    }
    return on_file | this_chat


def item_returnability(session: SessionState, order: dict, item: dict) -> dict:
    """Whether one item can be returned, decided by policy.py, with a plain-language reason if not."""
    reason = ineligible_reason(order, item, TODAY, returned_item_ids(session, order))
    if reason is None:
        return {"returnable": True}
    return {"returnable": False, "reason_code": reason, "explanation": INELIGIBLE_EXPLANATIONS[reason]}


def get_order(session: SessionState, order_id: str) -> dict:
    """Return one order's full details, if it belongs to the verified customer."""
    if session.verified_customer_id is None:
        return NOT_VERIFIED

    order = find_order(session, order_id)
    if order is None:
        return order_not_found(order_id)  # same answer for someone else's order and a fake one, so nothing leaks

    details = {k: v for k, v in order.items() if k != "customer_id"}
    details["items"] = [{**item, **item_returnability(session, order, item)} for item in order["items"]]
    return tool_ok(order=details, status=overall_status(order))
