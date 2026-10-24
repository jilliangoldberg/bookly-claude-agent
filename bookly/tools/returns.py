"""Return tools, split in two on purpose: prepare_return prices a return and stores a quote;
submit_return acts on it, but only after the customer has replied. That split is how the
agent is forced to confirm before committing, in code rather than by asking nicely in the prompt."""

from bookly.config import RESOLUTION_TIMING, TODAY
from bookly.models import Quote, SessionState, tool_error, tool_ok
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

SUBMIT_RETURN_SCHEMA = {
    "name": "submit_return",
    "description": (
        "Submit a quoted return and issue an RMA number. Only call this after the customer has clearly said "
        "yes to the quote in a new message. If they change anything, call prepare_return again instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"quote_id": {"type": "string", "description": "The quote_id from prepare_return"}},
        "required": ["quote_id"],
        "additionalProperties": False,
    },
}


def prepare_return(session: SessionState, order_id: str, item_ids: list[str], reason: str, resolution: str) -> dict:
    """Check each item against policy, price the eligible ones, and store a quote for later submission."""
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
        reason=reason,
        resolution=resolution,
        created_turn=session.turn_index,
        **amounts,
    )
    session.quotes[quote.quote_id] = quote
    return tool_ok(
        quote_id=quote.quote_id,
        eligible_items=[{"item_id": i["item_id"], "title": i["title"], "price": i["price"]} for i in eligible],
        ineligible_items=ineligible,
        resolution=resolution,
        timing=RESOLUTION_TIMING[resolution],
        **amounts,
        next_step="Read this quote back to the customer and ask them to confirm before calling submit_return.",
    )


def submit_return(session: SessionState, quote_id: str) -> dict:
    """Turn a confirmed quote into an RMA. Refuses if the customer hasn't replied since the quote."""
    quote = session.quotes.get(quote_id)
    if quote is None:
        return tool_error(
            "QUOTE_NOT_FOUND",
            "I need to price the return before submitting it.",
            hint="Call prepare_return first and use the quote_id it returns.",
        )
    if quote.rma_number:  # idempotent: a repeat call returns the same RMA instead of a second return
        return tool_ok(rma_number=quote.rma_number, already_submitted=True)
    if session.turn_index <= quote.created_turn:
        return tool_error(
            "CONFIRMATION_REQUIRED",
            "I need your OK on these details before I submit the return.",
            hint="Read the quote back to the customer and stop. Submit only after they say yes in a new message.",
        )

    quote.rma_number = f"RMA-{quote.order_id.removeprefix('BK-')}-{quote.quote_id.removeprefix('Q-')}"
    label = "prepaid return label" if quote.label == "prepaid" else f"return label (${quote.fee:.2f} deducted from refund)"
    return tool_ok(
        rma_number=quote.rma_number,
        refund_amount=quote.refund_amount,
        resolution=quote.resolution,
        label_url=f"https://bookly.example.com/labels/{quote.rma_number}.pdf",  # mock
        next_steps=f"We've emailed a {label}. Pack the book(s) and drop them off within 14 days. "
                   + RESOLUTION_TIMING[quote.resolution],
    )
