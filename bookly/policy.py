"""Return rules: who can return what, and for how much. Pure functions, no I/O, no LLM.
This is where Bookly's commitments are decided, so it's the most heavily tested file."""

from datetime import date

from bookly.config import EBOOK_REFUND_DAYS, RETURN_LABEL_FEE, RETURN_WINDOW_DAYS

# Plain-language reason for each "no", so Dewey explains decisions it didn't make itself.
INELIGIBLE_EXPLANATIONS = {
    "PREORDER": "Preorders haven't shipped yet, so there's nothing to return. Cancellations go to a teammate.",
    "NOT_DELIVERED": "This item hasn't been delivered yet, so it can't be returned until it arrives.",
    "OUT_OF_WINDOW": f"Books can be returned within {RETURN_WINDOW_DAYS} days of delivery, and this one is past that.",
    "EBOOK_DOWNLOADED": "Ebooks can only be refunded if they were never downloaded, and this one was.",
    "EBOOK_WINDOW_PASSED": f"Ebooks can be refunded within {EBOOK_REFUND_DAYS} days of purchase, and this one is past that.",
    "ALREADY_RETURNED": "A return has already been started for this item.",
}


def days_since(iso_date: str, today: date) -> int:
    """Whole days from an ISO date (e.g. a delivery date) to today."""
    return (today - date.fromisoformat(iso_date)).days


def ineligible_reason(order: dict, item: dict, today: date, returned_item_ids: set[str]) -> str | None:
    """Return why an item can't be returned (a reason code), or None if it can."""
    if item["item_id"] in returned_item_ids:
        return "ALREADY_RETURNED"
    if order["order_type"] == "preorder":
        return "PREORDER"
    if order["order_type"] == "digital":
        if item["downloaded"]:
            return "EBOOK_DOWNLOADED"
        if days_since(item["purchased_date"], today) > EBOOK_REFUND_DAYS:
            return "EBOOK_WINDOW_PASSED"
        return None

    shipment = next(s for s in order["shipments"] if s["shipment_id"] == item["shipment_id"])
    if shipment["status"] != "delivered":
        return "NOT_DELIVERED"
    if days_since(shipment["delivered_date"], today) > RETURN_WINDOW_DAYS:  # day 30 is still allowed
        return "OUT_OF_WINDOW"
    return None


def resolution_allowed(reason: str, resolution: str) -> bool:
    """Replacements are only for damaged or wrong items; every reason can take a refund or credit."""
    return resolution != "replacement" or reason in ("damaged", "wrong_item")


def quote_amounts(items: list[dict], reason: str, resolution: str) -> dict:
    """Compute refund, fee, and label type for the eligible items of one return."""
    subtotal = round(sum(item["price"] for item in items), 2)  # floats are fine at demo scale; real money would use cents

    if reason in ("damaged", "wrong_item"):  # our mistake: no fee, prepaid label
        refund = 0.0 if resolution == "replacement" else subtotal
        return {"refund_amount": refund, "fee": 0.0, "label": "prepaid"}
    if resolution == "store_credit":  # change of mind, kept as credit: full amount, no fee
        return {"refund_amount": subtotal, "fee": 0.0, "label": "prepaid"}
    # Change of mind back to the original payment: the label fee comes out of the refund.
    return {"refund_amount": round(subtotal - RETURN_LABEL_FEE, 2), "fee": RETURN_LABEL_FEE, "label": "fee_deducted"}
