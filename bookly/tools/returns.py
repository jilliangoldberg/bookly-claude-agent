"""Return proposal tool. It prices a return for the UI but can never submit one."""

from bookly.config import RESOLUTION_TIMING, TODAY
from bookly.models import PendingAction, Quote, SessionState, tool_error, tool_ok
from bookly.policy import INELIGIBLE_EXPLANATIONS, ineligible_reason, quote_amounts, resolution_allowed
from bookly.tools.orders import NOT_VERIFIED, find_order, order_not_found, returned_item_ids

PREPARE_RETURN_SCHEMA = {
    "name": "prepare_return",
    "description": (
        "Price a return and create a quote. Does NOT submit anything. Call it only once you know the order, "
        "which items, the reason, and the resolution the customer wants. Then read the quote back to the "
        "customer (items, amount, fee, label, timing) and ask them to confirm. Amounts come only from this tool."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "Order id like BK-1001"},
            "item_ids": {"type": "array", "items": {"type": "string"}, "description": "Item ids like I-2"},
            "reason": {"type": "string", "enum": ["changed_mind", "damaged", "wrong_item"]},
            "resolution": {
                "type": "string",
                "enum": ["refund_original", "store_credit", "replacement"],
                "description": "replacement is only allowed for damaged or wrong items",
            },
        },
        "required": ["order_id", "item_ids", "reason", "resolution"],
        "additionalProperties": False,
    },
}

def prepare_return(session: SessionState, order_id: str, item_ids: list[str], reason: str, resolution: str) -> dict:
    """Check policy, price eligible items, and stage an immutable UI-authorized proposal."""
    session.pending_action = None
    if session.verified_customer_id is None:
        return NOT_VERIFIED
    order = find_order(session, order_id)
    if order is None:
        return order_not_found(order_id)
    if not resolution_allowed(reason, resolution):
        return tool_error(
            "INVALID_RESOLUTION",
            "Replacements are only for damaged or wrong items.",
            hint="Offer a refund to the original payment or store credit instead.",
        )

    items_by_id = {item["item_id"]: item for item in order["items"]}
    unknown = [i for i in item_ids if i not in items_by_id]
    if unknown or not item_ids:
        return tool_error(
            "ITEM_NOT_IN_ORDER",
            "I couldn't match that book to this order.",
            hint=f"Use item ids from get_order for {order['order_id']}; these weren't in it: {unknown or 'none given'}.",
        )

    already_returned = returned_item_ids(session, order)
    eligible, ineligible = [], []
    for item_id in item_ids:
        item = items_by_id[item_id]
        code = ineligible_reason(order, item, TODAY, already_returned)
        if code is None:
            eligible.append(item)
        else:
            ineligible.append({"item_id": item_id, "title": item["title"], "reason_code": code,
                               "explanation": INELIGIBLE_EXPLANATIONS[code]})

    if not eligible:
        ineligible_error = tool_error(
            "INELIGIBLE",
            "None of these items can be returned.",
            hint="Explain each item's explanation kindly. If they want an exception, offer escalate_to_human.",
        )
        return {**ineligible_error, "ineligible_items": ineligible}

    amounts = quote_amounts(eligible, reason, resolution)
    quote = Quote(
        quote_id=f"Q-{len(session.quotes) + 1}",
        order_id=order["order_id"],
        item_ids=[item["item_id"] for item in eligible],
        item_titles=[item["title"] for item in eligible],
        reason=reason,
        resolution=resolution,
        timing=RESOLUTION_TIMING[resolution],
        created_turn=session.turn_index,
        **amounts,
    )
    session.quotes[quote.quote_id] = quote
    pending = PendingAction(
        token=f"PA-{len(session.quotes)}",
        kind="submit_return",
        customer_id=session.verified_customer_id,
        quote_id=quote.quote_id,
        created_turn=session.turn_index,
    )
    session.pending_action = pending
    return tool_ok(
        quote_id=quote.quote_id,
        eligible_items=[{"item_id": i["item_id"], "title": i["title"], "price": i["price"]} for i in eligible],
        ineligible_items=ineligible,
        resolution=resolution,
        timing=quote.timing,
        **amounts,
        pending_action_token=pending.token,
        next_step="Read this quote back, then tell the customer to use the Confirm return or Cancel buttons.",
    )
