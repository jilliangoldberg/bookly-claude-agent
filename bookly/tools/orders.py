"""Order tools: read-only lookups, scoped to the verified customer. The customer ID always
comes from the session, never from tool input, so the LLM can't ask for someone else's orders."""

from bookly import data
from bookly.config import TODAY
from bookly.models import SessionState, tool_error, tool_ok
from bookly.policy import INELIGIBLE_EXPLANATIONS, ineligible_reason

NOT_VERIFIED = tool_error("NOT_VERIFIED", "The customer must be verified with email and ZIP first.")

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
        "properties": {"order_id": {"type": "string", "description": "Order id like BK-1001"}},
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
    wanted = order_id.strip().upper()
    return next(
        (o for o in data.orders() if o["order_id"] == wanted and o["customer_id"] == session.verified_customer_id),
        None,
    )


def returned_item_ids(session: SessionState, order: dict) -> set[str]:
    """Items with a return already on file or submitted earlier in this chat."""
    on_file = {item_id for r in order["returns"] for item_id in r["item_ids"]}
    this_chat = {
        item_id
        for q in session.quotes.values()
        if q.order_id == order["order_id"] and q.rma_number
        for item_id in q.item_ids
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
        # Same answer whether the order is someone else's or doesn't exist, so nothing leaks.
        return tool_error("NOT_FOUND", f"No order {order_id.strip().upper()} on this account.")

    details = {k: v for k, v in order.items() if k != "customer_id"}
    details["items"] = [{**item, **item_returnability(session, order, item)} for item in order["items"]]
    return tool_ok(order=details, status=overall_status(order))
