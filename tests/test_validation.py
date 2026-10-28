"""Tests for the reply check in validation.py: every amount, date, id, and completed-action claim in a
draft must trace to trusted session data. Pure and free: real tools, no LLM."""

import pytest

from bookly.actions import confirm_return
from bookly.models import SessionState, TraceEvent
from bookly.tools import dispatch
from bookly.validation import check_reply, collect_evidence

MAYA = {"email": "maya.chen@example.com", "zip_code": "94110"}


def run(session: SessionState, name: str, args: dict) -> dict:
    """Run a real tool and record it in the trace, the way the agent loop does."""
    output = dispatch(name, args, session)
    session.trace.append(TraceEvent(turn=session.turn_index, kind="tool", name=name, input=args,
                                    output=output, ok=output.get("ok", False), latency_ms=0))
    return output


def maya_with_quote() -> SessionState:
    """Maya verified, BK-1001 looked up, and a change-of-mind refund quoted for Salt & Static ($22.01)."""
    session = SessionState(turn_index=1)
    run(session, "verify_customer", MAYA)
    run(session, "get_order", {"order_id": "BK-1001"})
    run(session, "prepare_return", {"order_id": "BK-1001", "item_ids": ["I-2"], "reason": "changed_mind",
                                    "resolution": "refund_original"})
    return session


def codes(reply: str, session: SessionState) -> list[str]:
    return [v.code for v in check_reply(reply, collect_evidence(session))]


# --- Amounts ---

def test_quoted_amounts_pass():
    session = maya_with_quote()
    assert codes("You'd get $22.01 back after the $4.99 label fee.", session) == []


@pytest.mark.parametrize("amount", ["$22", "$27.00", "$27"])
def test_amounts_are_compared_in_cents(amount):
    # Salt & Static is $27.00 in the order, so "$27" and "$27.00" are the same fact; $22 is not $22.01.
    session = maya_with_quote()
    assert codes(f"That's {amount}.", session) == ([] if amount != "$22" else ["UNSUPPORTED_AMOUNT"])


def test_a_quote_subtotal_is_allowed():
    session = maya_with_quote()  # $22.01 refund + $4.99 fee
    assert codes("The book was $27.00; minus $4.99 you get $22.01.", session) == []


def test_an_invented_amount_is_flagged():
    assert codes("Your refund is $150.", maya_with_quote()) == ["UNSUPPORTED_AMOUNT"]


def test_an_amount_the_customer_typed_is_not_evidence():
    session = maya_with_quote()
    session.messages.append({"role": "user", "content": "Is my refund $30?"})
    assert codes("Yes, your refund is $30.", session) == ["UNSUPPORTED_AMOUNT"]


def test_policy_amounts_count_only_after_a_search_returned_them():
    session = SessionState(turn_index=1)
    assert codes("Expedited shipping is $9.99.", session) == ["UNSUPPORTED_AMOUNT"]
    run(session, "search_policies", {"query": "expedited shipping cost"})
    assert codes("Expedited shipping is $9.99.", session) == []


# --- Dates ---

@pytest.mark.parametrize("written", ["Oct 14", "October 14", "Oct 14, 2026", "10/14", "2026-10-14", "Oct. 14th"])
def test_dates_match_in_any_common_format(written):
    session = maya_with_quote()  # BK-1001 was delivered 2026-10-14
    assert codes(f"It was delivered {written}.", session) == []


def test_a_wrong_day_or_year_is_flagged():
    session = maya_with_quote()
    assert codes("It was delivered Oct 15.", session) == ["UNSUPPORTED_DATE"]
    assert codes("It was delivered Oct 14, 2025.", session) == ["UNSUPPORTED_DATE"]


def test_today_is_always_known():
    assert codes("Today is Oct 24.", SessionState()) == []


def test_capitalized_month_names_only():
    assert codes("You may 3 times try again.", SessionState()) == []


# --- Ids ---

def test_ids_from_tool_results_pass_and_made_up_ones_are_flagged():
    session = maya_with_quote()
    assert codes("Order BK-1001, tracking 1Z999AA10123450001.", session) == []
    assert codes("Tracking 1Z999AA10123459999.", session) == ["UNSUPPORTED_ID"]
    assert codes("Your order BK-1004 shipped.", session) == ["UNSUPPORTED_ID"]


def test_hint_text_is_not_evidence():
    session = SessionState(turn_index=1)
    run(session, "verify_customer", MAYA)
    output = run(session, "prepare_return", {"order_id": "BK-1001", "item_ids": ["I-9"], "reason": "changed_mind",
                                             "resolution": "refund_original"})
    assert "I-9" in output["hint"]  # the hint echoes the model's own made-up item id
    assert codes("I'll return item I-9 for you.", session) == ["UNSUPPORTED_ID"]


# --- Completed-action claims ---

def test_claiming_a_submitted_return_needs_a_confirm():
    session = maya_with_quote()
    claim = "I've submitted your return."
    assert codes(claim, session) == ["UNSUPPORTED_ACTION"]
    confirm_return(session, session.pending_action.token)
    assert codes(claim, session) == []


@pytest.mark.parametrize("text", [
    "It will be submitted once you click Confirm return.",
    "Your return hasn't been submitted yet.",
    "I can't submit returns from chat.",
    "Once you confirm, we'll email you a label.",
    "I can pass this to a teammate if you'd like.",
])
def test_future_conditional_and_negated_claims_pass(text):
    assert codes(text, maya_with_quote()) == []


def test_claiming_an_escalation_needs_a_ticket():
    session = maya_with_quote()
    claim = "I've passed this to a teammate."
    assert codes(claim, session) == ["UNSUPPORTED_ACTION"]
    run(session, "escalate_to_human", {"reason": "customer_request", "summary": "Maya wants a person to review BK-1001."})
    assert codes(claim, session) == []


def test_claiming_a_cancellation_needs_a_cancel():
    assert codes("I've cancelled your order.", maya_with_quote()) == ["UNSUPPORTED_ACTION"]


def test_claiming_verification_needs_a_verified_session():
    assert codes("I've verified your account.", SessionState()) == ["UNSUPPORTED_ACTION"]
    assert codes("I've verified your account.", maya_with_quote()) == []
