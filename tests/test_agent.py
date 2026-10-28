"""Tests for the orchestration loop in agent.py, using a scripted fake client instead of Claude.
They check the plumbing: tool round trips, parallel calls, failure handling, and history hygiene.
Deterministic and free: no network, no API key."""

from types import SimpleNamespace

import pytest

from bookly.actions import confirm_return
from bookly.agent import handle_user_message
from bookly.config import MAX_AGENT_STEPS, MODEL
from bookly.models import SessionState
from bookly.tools import dispatch
from bookly.validation import SAFE_FALLBACK
from tests.fakes import FakeClient, text_reply, tool_calls


def test_plain_reply_is_returned():
    fake = FakeClient(text_reply("Hi! How can I help?"))
    assert handle_user_message(SessionState(), "hello", api=fake) == "Hi! How can I help?"


def test_request_shape():
    fake = FakeClient(text_reply("ok"))
    handle_user_message(SessionState(), "hello", api=fake)
    request = fake.requests[0]
    assert request["model"] == MODEL
    assert request["output_config"] == {"effort": "low"}
    assert request["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in request and "tool_choice" not in request  # neither is accepted on this model
    assert "Dewey" in request["system"]


def test_successful_application_action_is_trusted_system_context():
    session = SessionState(turn_index=1)
    dispatch("verify_customer", {"email": "maya.chen@example.com", "zip_code": "94110"}, session)
    dispatch(
        "prepare_return",
        {
            "order_id": "BK-1001",
            "item_ids": ["I-2"],
            "reason": "changed_mind",
            "resolution": "refund_original",
        },
        session,
    )
    result = confirm_return(session, session.pending_action.token)

    fake = FakeClient(text_reply(f"Your RMA is {result['rma_number']}."))
    handle_user_message(session, "What is my RMA?", api=fake)
    assert "Trusted application events" in fake.requests[0]["system"]
    assert result["rma_number"] in fake.requests[0]["system"]


def test_tool_result_is_sent_back_and_the_loop_continues():
    fake = FakeClient(
        tool_calls(("search_policies", {"query": "ship to Canada"})),
        text_reply("Yes, we ship to Canada."),
    )
    session = SessionState()
    assert handle_user_message(session, "Do you ship to Canada?", api=fake) == "Yes, we ship to Canada."

    tool_message = fake.requests[1]["messages"][-1]
    assert tool_message["role"] == "user"
    assert tool_message["content"][0]["tool_use_id"] == "call_0"
    assert '"ship-02"' in tool_message["content"][0]["content"]
    assert [e.kind for e in session.trace] == ["llm", "tool", "llm"]


def test_parallel_tool_calls_return_together_in_one_message():
    fake = FakeClient(
        tool_calls(("search_policies", {"query": "ship to Canada"}), ("search_policies", {"query": "price match"})),
        text_reply("Two answers."),
    )
    handle_user_message(SessionState(), "two questions", api=fake)
    results = fake.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["call_0", "call_1"]


def test_on_step_reports_each_tool_before_it_runs():
    fake = FakeClient(
        tool_calls(("search_policies", {"query": "ship to Canada"}), ("list_orders", {})),
        text_reply("Done."),
    )
    session = SessionState()
    seen = []
    on_step = lambda name, tool_input: seen.append((name, tool_input, len(session.trace)))
    assert handle_user_message(session, "hi", api=fake, on_step=on_step) == "Done."
    # One LLM event is traced before the first tool; each tool is reported before its own trace event.
    assert seen == [("search_policies", {"query": "ship to Canada"}, 1), ("list_orders", {}, 2)]


def test_failed_tool_is_marked_as_an_error():
    fake = FakeClient(tool_calls(("list_orders", {})), text_reply("Let's verify you first."))
    handle_user_message(SessionState(), "my orders?", api=fake)
    result = fake.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "NOT_VERIFIED" in result["content"]


def test_chat_cannot_submit_and_a_new_message_invalidates_the_proposal():
    """Even a fabricated submit tool call cannot cross the application action boundary."""
    session = SessionState()
    verify = ("verify_customer", {"email": "maya.chen@example.com", "zip_code": "94110"})
    prepare = ("prepare_return", {"order_id": "BK-1001", "item_ids": ["I-2"], "reason": "changed_mind", "resolution": "refund_original"})
    fake = FakeClient(
        tool_calls(verify), tool_calls(prepare), text_reply("Use the Confirm return button."),
    )
    handle_user_message(session, "Return Salt & Static, changed my mind, refund please. maya.chen@example.com 94110", api=fake)
    token = session.pending_action.token

    fake = FakeClient(tool_calls(("submit_return", {"quote_id": "Q-1"})), text_reply("I cannot submit it in chat."))
    handle_user_message(session, "yes", api=fake)
    submit = [e for e in session.trace if e.name == "submit_return"][0]
    assert submit.output["error_code"] == "UNKNOWN_TOOL"
    assert session.pending_action is None
    assert session.action_results[token]["status"] == "invalidated"
    assert not any(result.get("status") == "submitted" for result in session.action_results.values())


def test_api_error_rolls_back_the_turn():
    session = SessionState(messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
    fake = FakeClient(tool_calls(("list_orders", {})), RuntimeError("API down"))
    with pytest.raises(RuntimeError):
        handle_user_message(session, "where's my order?", api=fake)
    # No dangling tool call left behind to break the next request.
    assert session.messages == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
def test_incomplete_reply_is_never_shown_and_opens_a_ticket(stop_reason):
    session = SessionState()
    fake = FakeClient(text_reply("Your refund is $9", stop_reason=stop_reason))
    reply = handle_user_message(session, "refund me", api=fake)
    assert "$9" not in reply and session.escalation["ticket_id"] in reply
    # History holds only what the customer actually saw.
    assert session.messages == [{"role": "user", "content": "refund me"}, {"role": "assistant", "content": reply}]


def test_refusal_category_is_recorded_in_the_trace():
    response = text_reply("", stop_reason="refusal")
    response.stop_details = SimpleNamespace(category="general_harms")
    session = SessionState()
    handle_user_message(session, "x", api=FakeClient(response))
    assert session.trace[0].output["refusal_category"] == "general_harms"


def test_step_limit_hands_off_instead_of_looping_forever():
    session = SessionState()
    fake = FakeClient(*[tool_calls(("list_orders", {})) for _ in range(MAX_AGENT_STEPS)])
    reply = handle_user_message(session, "loop", api=fake)
    assert len(fake.requests) == MAX_AGENT_STEPS
    assert session.escalation["reason"] == "agent_stuck" and "ticket" in reply


# --- The reply check: drafts with unsupported facts never reach the customer ---

def test_a_clean_draft_is_appended_unchanged_with_no_extra_call():
    draft = text_reply("Happy to help with your order!")
    fake = FakeClient(draft)
    session = SessionState()
    assert handle_user_message(session, "hi", api=fake) == "Happy to help with your order!"
    assert len(fake.requests) == 1
    assert session.messages[-1] == {"role": "assistant", "content": draft.content}  # thinking blocks kept as-is


def test_an_unsupported_draft_gets_one_rewrite_with_tools_off():
    fake = FakeClient(text_reply("Your refund of $150 is on its way."), text_reply("I can check refund amounts once I find your order."))
    session = SessionState()
    reply = handle_user_message(session, "where's my refund?", api=fake)
    assert reply == "I can check refund amounts once I find your order."
    repair = fake.requests[1]
    assert repair["tool_choice"] == {"type": "none"}
    assert repair["tools"] == fake.requests[0]["tools"]  # same tools, so the cached prefix still matches
    assert repair["messages"][-1]["role"] == "system" and "$150" in repair["messages"][-1]["content"]
    # History holds only what the customer saw, as plain text; the draft and the instruction never enter it.
    assert session.messages == [{"role": "user", "content": "where's my refund?"}, {"role": "assistant", "content": reply}]
    [check] = [e for e in session.trace if e.kind == "validation"]
    assert check.ok and check.output["outcome"] == "repaired"
    assert check.output["violations"] == ["UNSUPPORTED_AMOUNT: '$150'"]


def test_a_rewrite_that_still_fails_sends_the_safe_fallback():
    fake = FakeClient(text_reply("I've submitted your return."), text_reply("Done, it's submitted: RMA-1001-1."))
    session = SessionState()
    assert handle_user_message(session, "return it", api=fake) == SAFE_FALLBACK
    assert session.messages[-1] == {"role": "assistant", "content": SAFE_FALLBACK}
    [check] = [e for e in session.trace if e.kind == "validation"]
    assert not check.ok and check.output["outcome"] == "fallback" and check.output["repair_violations"]


def test_an_empty_or_cut_off_rewrite_sends_the_safe_fallback():
    fake = FakeClient(text_reply("Your refund is $150."), text_reply("Your ref", stop_reason="max_tokens"))
    assert handle_user_message(SessionState(), "refund?", api=fake) == SAFE_FALLBACK


def test_an_api_error_during_the_rewrite_rolls_back_the_turn():
    session = SessionState(messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
    fake = FakeClient(text_reply("Your refund is $150."), RuntimeError("API down"))
    with pytest.raises(RuntimeError):
        handle_user_message(session, "refund?", api=fake)
    assert session.messages == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]


def test_on_step_reports_the_reply_check():
    steps = []
    fake = FakeClient(text_reply("Your refund is $150."), text_reply("Let me look that up first."))
    handle_user_message(SessionState(), "refund?", api=fake, on_step=lambda name, args: steps.append(name))
    assert steps == ["check_reply"]
