"""Order tools: read-only lookups, scoped to the verified customer. The customer ID always
comes from the session, never from tool input, so the LLM can't ask for someone else's orders."""

from bookly import data
from bookly.models import SessionState, tool_error, tool_ok

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
        "carrier, tracking number, ETA or delivered date, and preorder release date. "
        "Use it before reporting any order status."
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


def get_order(session: SessionState, order_id: str) -> dict:
    """Return one order's full details, if it belongs to the verified customer."""
    if session.verified_customer_id is None:
        return NOT_VERIFIED

    wanted = order_id.strip().upper()
    order = next(
        (o for o in data.orders() if o["order_id"] == wanted and o["customer_id"] == session.verified_customer_id),
        None,
    )
    if order is None:
        # Same answer whether the order is someone else's or doesn't exist, so nothing leaks.
        return tool_error("NOT_FOUND", f"No order {wanted} on this account.")

    details = {k: v for k, v in order.items() if k != "customer_id"}
    return tool_ok(order=details, status=overall_status(order))
