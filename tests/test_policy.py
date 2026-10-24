"""Unit tests for the return rules. No LLM, no network; runs in well under a second."""

from datetime import date

from bookly import data
from bookly.policy import ineligible_reason, quote_amounts, resolution_allowed

TODAY = date(2026, 10, 24)


def order_by_id(order_id: str) -> dict:
    """Fetch one mock order by id."""
    return next(o for o in data.orders() if o["order_id"] == order_id)


def delivered_order(delivered_date: str) -> dict:
    """A synthetic one-item order delivered on the given date."""
    return {
        "order_type": "standard",
        "items": [{"item_id": "X-1", "price": 10.0, "shipment_id": "S1"}],
        "shipments": [{"shipment_id": "S1", "status": "delivered", "delivered_date": delivered_date}],
    }


def ebook_order(purchased_date: str, downloaded: bool) -> dict:
    """A synthetic one-ebook order."""
    return {
        "order_type": "digital",
        "items": [{"item_id": "E-1", "price": 9.99, "purchased_date": purchased_date, "downloaded": downloaded}],
        "shipments": [],
    }


def reason_for(order: dict, item_index: int = 0, returned: set[str] = frozenset()) -> str | None:
    """Shortcut: eligibility reason for one item of an order."""
    return ineligible_reason(order, order["items"][item_index], TODAY, set(returned))


# --- Eligibility ---

def test_day_30_after_delivery_is_eligible():
    assert reason_for(delivered_order("2026-09-24")) is None


def test_day_31_after_delivery_is_out_of_window():
    assert reason_for(delivered_order("2026-09-23")) == "OUT_OF_WINDOW"


def test_recent_delivery_is_eligible():
    assert reason_for(order_by_id("BK-1001")) is None


def test_old_delivery_is_out_of_window():
    assert reason_for(order_by_id("BK-1003")) == "OUT_OF_WINDOW"


def test_in_transit_item_is_not_delivered():
    assert reason_for(order_by_id("BK-1005"), item_index=1) == "NOT_DELIVERED"


def test_preorder_is_not_returnable():
    assert reason_for(order_by_id("BK-1007")) == "PREORDER"


def test_downloaded_ebook_is_not_refundable():
    assert reason_for(order_by_id("BK-1004")) == "EBOOK_DOWNLOADED"


def test_undownloaded_ebook_on_day_7_is_eligible():
    assert reason_for(ebook_order("2026-10-17", downloaded=False)) is None


def test_undownloaded_ebook_on_day_8_is_past_window():
    assert reason_for(ebook_order("2026-10-16", downloaded=False)) == "EBOOK_WINDOW_PASSED"


def test_item_already_returned():
    assert reason_for(order_by_id("BK-1001"), returned={"I-1"}) == "ALREADY_RETURNED"


# --- Resolutions ---

def test_replacement_only_for_damaged_or_wrong_item():
    assert resolution_allowed("damaged", "replacement")
    assert resolution_allowed("wrong_item", "replacement")
    assert not resolution_allowed("changed_mind", "replacement")


# --- Amounts ---

SALT_AND_STATIC = [{"price": 27.00}]


def test_change_of_mind_refund_deducts_label_fee():
    assert quote_amounts(SALT_AND_STATIC, "changed_mind", "refund_original") == {
        "refund_amount": 22.01, "fee": 4.99, "label": "fee_deducted",
    }


def test_change_of_mind_store_credit_is_full_amount():
    assert quote_amounts(SALT_AND_STATIC, "changed_mind", "store_credit")["refund_amount"] == 27.00


def test_damaged_refund_is_full_with_prepaid_label():
    assert quote_amounts(SALT_AND_STATIC, "damaged", "refund_original") == {
        "refund_amount": 27.00, "fee": 0.0, "label": "prepaid",
    }


def test_replacement_refunds_nothing():
    assert quote_amounts(SALT_AND_STATIC, "damaged", "replacement")["refund_amount"] == 0.0


def test_fee_charged_once_per_return_not_per_item():
    two_books = [{"price": 16.99}, {"price": 27.00}]
    assert quote_amounts(two_books, "changed_mind", "refund_original")["refund_amount"] == 39.00
