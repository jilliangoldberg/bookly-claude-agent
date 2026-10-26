"""Guardrail tests: the rules that live in tool code, so no prompt can talk its way around them.
Tools are called directly through dispatch(), exactly as the agent calls them. No LLM."""

import pytest

from bookly import data
from bookly.actions import cancel_return, confirm_return
from bookly.models import SessionState
from bookly.tools import TOOL_SCHEMAS, dispatch

MAYA = {"email": "maya.chen@example.com", "zip_code": "94110"}


def verified_maya() -> SessionState:
    """A session where Maya has already verified, on her first message."""
    session = SessionState(turn_index=1)
    assert dispatch("verify_customer", MAYA, session)["ok"]
    return session


def maya_quote(session: SessionState, resolution: str = "refund_original") -> dict:
    """Prepare S3's return: Salt & Static, changed mind."""
    return dispatch(
        "prepare_return",
        {"order_id": "BK-1001", "item_ids": ["I-2"], "reason": "changed_mind", "resolution": resolution},
        session,
    )


# --- Identity ---

@pytest.mark.parametrize("tool, args", [
    ("list_orders", {}),
    ("get_order", {"order_id": "BK-1001"}),
    ("prepare_return", {"order_id": "BK-1001", "item_ids": ["I-2"], "reason": "changed_mind", "resolution": "store_credit"}),
])
def test_order_tools_refuse_before_verification(tool, args):
    assert dispatch(tool, args, SessionState())["error_code"] == "NOT_VERIFIED"


def test_wrong_email_and_wrong_zip_look_identical():
    wrong_zip = dispatch("verify_customer", {**MAYA, "zip_code": "00000"}, SessionState())
    wrong_email = dispatch("verify_customer", {**MAYA, "email": "nobody@example.com"}, SessionState())
    assert wrong_zip == wrong_email


def test_verification_locks_after_three_failures():
    session = SessionState()
    results = [dispatch("verify_customer", {**MAYA, "zip_code": "00000"}, session) for _ in range(4)]
    assert [r["error_code"] for r in results] == ["VERIFY_FAILED"] * 3 + ["VERIFY_LOCKED"]
    # Even the right details don't work once locked.
    assert dispatch("verify_customer", MAYA, session)["error_code"] == "VERIFY_LOCKED"


def test_successful_verification_cannot_be_replaced_mid_session():
    session = verified_maya()
    result = dispatch(
        "verify_customer",
        {"email": "daniel.okafor@example.com", "zip_code": "10027"},
        session,
    )
    assert result["already_verified"] is True
    assert session.verified_customer_id == result["customer_id"] == "C001"
    assert session.verified_first_name == result["first_name"] == "Maya"


def test_other_customers_order_looks_like_a_missing_one():
    session = verified_maya()
    someone_elses = dispatch("get_order", {"order_id": "BK-1003"}, session)  # Daniel's order
    nonexistent = dispatch("get_order", {"order_id": "BK-1003".replace("1003", "9999")}, session)
    assert someone_elses["error_code"] == nonexistent["error_code"] == "NOT_FOUND"
    assert someone_elses["hint"] == nonexistent["hint"]
    assert someone_elses["message"].replace("1003", "X") == nonexistent["message"].replace("9999", "X")


def test_list_orders_only_shows_the_verified_customer():
    orders = dispatch("list_orders", {}, verified_maya())["orders"]
    assert [o["order_id"] for o in orders] == ["BK-1001", "BK-1002"]


@pytest.mark.parametrize("typed", ["BK-1001", "1001", "#bk-1001", "BK 1001"])
def test_order_ids_are_normalized_in_code(typed):
    assert dispatch("get_order", {"order_id": typed}, verified_maya())["ok"]


# --- Returns: application confirmation before commit ---

def test_quote_amounts_come_from_code():
    quote = maya_quote(verified_maya())
    assert (quote["refund_amount"], quote["fee"], quote["label"]) == (22.01, 4.99, "fee_deducted")


def test_submit_is_not_a_model_tool():
    session = verified_maya()
    maya_quote(session)
    assert "submit_return" not in {schema["name"] for schema in TOOL_SCHEMAS}
    assert dispatch("submit_return", {"quote_id": "Q-1"}, session)["error_code"] == "UNKNOWN_TOOL"
    assert session.action_results == {}


def test_ui_confirmation_succeeds_and_is_idempotent():
    session = verified_maya()
    maya_quote(session)
    token = session.pending_action.token
    first = confirm_return(session, token)
    second = confirm_return(session, token)
    assert first["ok"] and second["already_submitted"]
    assert first["rma_number"] == second["rma_number"]
    assert session.pending_action is None


def test_ui_confirmation_rejects_a_made_up_action():
    assert confirm_return(verified_maya(), "PA-99")["error_code"] == "ACTION_NOT_FOUND"


def test_ui_confirmation_rechecks_policy(monkeypatch):
    session = verified_maya()
    maya_quote(session)
    token = session.pending_action.token
    order = next(order for order in data.orders() if order["order_id"] == "BK-1001")
    monkeypatch.setitem(order["shipments"][0], "status", "in_transit")
    result = confirm_return(session, token)
    assert result["error_code"] == "ACTION_STALE"
    assert session.pending_action is None
    assert not any(r.get("status") == "submitted" for r in session.action_results.values())


def test_ui_cancel_clears_the_proposal_without_submitting():
    session = verified_maya()
    maya_quote(session)
    token = session.pending_action.token
    result = cancel_return(session, token)
    assert result["ok"] and result["status"] == "cancelled"
    assert session.pending_action is None
    assert not any(r.get("status") == "submitted" for r in session.action_results.values())


def test_returned_item_cannot_be_returned_again():
    session = verified_maya()
    maya_quote(session)
    confirm_return(session, session.pending_action.token)
    again = maya_quote(session, resolution="store_credit")
    assert again["error_code"] == "INELIGIBLE"
    assert again["ineligible_items"][0]["reason_code"] == "ALREADY_RETURNED"


def test_replacement_is_only_for_damaged_or_wrong_items():
    assert maya_quote(verified_maya(), resolution="replacement")["error_code"] == "INVALID_RESOLUTION"


def test_get_order_flags_items_that_cant_be_returned():
    daniel = SessionState()
    dispatch("verify_customer", {"email": "daniel.okafor@example.com", "zip_code": "10027"}, daniel)
    item = dispatch("get_order", {"order_id": "BK-1003"}, daniel)["order"]["items"][0]
    assert item["returnable"] is False and item["reason_code"] == "OUT_OF_WINDOW"


# --- Grounding and handoff ---

@pytest.mark.parametrize("query", ["Do you ship to Canada?", "ship to the UK"])
def test_policy_search_finds_where_we_ship_first(query):
    # The UK question should find ship-02 too: that policy is what says "US and Canada only".
    results = dispatch("search_policies", {"query": query}, SessionState())["results"]
    assert results[0]["section_id"] == "ship-02"


@pytest.mark.parametrize("query", ["sell used books", "loyalty program points", "book club discounts"])
def test_policy_search_returns_nothing_for_uncovered_topics(query):
    result = dispatch("search_policies", {"query": query}, SessionState())
    assert result["results"] == [] and result["note"] == "no matching policy"


def test_escalation_needs_a_real_summary_and_opens_one_ticket():
    session = SessionState()
    assert dispatch("escalate_to_human", {"reason": "customer_request", "summary": "help"}, session)["error_code"] == "SUMMARY_TOO_SHORT"
    first = dispatch("escalate_to_human", {"reason": "policy_exception", "summary": "Exception for BK-1003, out of window."}, session)
    second = dispatch("escalate_to_human", {"reason": "customer_request", "summary": "Customer asked for a person again."}, session)
    assert second["ticket_id"] == first["ticket_id"] and second["already_open"]


# --- Registry ---

def test_dispatch_never_raises():
    session = SessionState()
    assert dispatch("delete_everything", {}, session)["error_code"] == "UNKNOWN_TOOL"
    assert dispatch("get_order", {"wrong_arg": 1}, session)["error_code"] == "TOOL_ERROR"


def test_every_schema_is_valid_for_strict_mode():
    for schema in TOOL_SCHEMAS:
        params = schema["input_schema"]
        assert schema["strict"] is True
        assert params["additionalProperties"] is False
        assert set(params["required"]) == set(params["properties"]), schema["name"]
