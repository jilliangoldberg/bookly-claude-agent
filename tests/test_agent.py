"""Tests for the orchestration loop in agent.py, using a scripted fake client instead of Claude.
They check the plumbing: tool round trips, parallel calls, failure handling, and history hygiene.
Deterministic and free: no network, no API key."""

from types import SimpleNamespace

import pytest

from bookly.agent import handle_user_message
from bookly.config import MAX_AGENT_STEPS, MODEL
from bookly.models import SessionState

USAGE = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0)


def text_reply(text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    """A fake model response containing only text."""
    return SimpleNamespace(stop_reason=stop_reason, stop_details=None, usage=USAGE,
                           content=[SimpleNamespace(type="text", text=text)])


def tool_calls(*calls: tuple[str, dict]) -> SimpleNamespace:
    """A fake model response asking for one or more tools."""
    blocks = [SimpleNamespace(type="tool_use", id=f"call_{i}", name=name, input=args)
              for i, (name, args) in enumerate(calls)]
    return SimpleNamespace(stop_reason="tool_use", stop_details=None, usage=USAGE, content=blocks)


class FakeClient:
    """Plays back scripted responses (or raises scripted errors) and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self  # so the agent can call fake.messages.create(...)

    def create(self, **request):
        self.requests.append({**request, "messages": list(request["messages"])})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


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


def test_failed_tool_is_marked_as_an_error():
    fake = FakeClient(tool_calls(("list_orders", {})), text_reply("Let's verify you first."))
    handle_user_message(SessionState(), "my orders?", api=fake)
    result = fake.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "NOT_VERIFIED" in result["content"]


def test_confirmation_gate_holds_through_the_loop():
    """Even if the model tries to submit in the same turn it quoted, code refuses."""
    session = SessionState()
    verify = ("verify_customer", {"email": "maya.chen@example.com", "zip_code": "94110"})
    prepare = ("prepare_return", {"order_id": "BK-1001", "item_ids": ["I-2"], "reason": "changed_mind", "resolution": "refund_original"})
    fake = FakeClient(
        tool_calls(verify), tool_calls(prepare), tool_calls(("submit_return", {"quote_id": "Q-1"})),
        text_reply("Shall I submit this return?"),
    )
    handle_user_message(session, "Return Salt & Static, changed my mind, refund please. maya.chen@example.com 94110", api=fake)
    submit = [e for e in session.trace if e.name == "submit_return"][0]
    assert submit.output["error_code"] == "CONFIRMATION_REQUIRED"
    assert session.quotes["Q-1"].rma_number is None

    fake = FakeClient(tool_calls(("submit_return", {"quote_id": "Q-1"})), text_reply("Done! RMA-1001-1."))
    handle_user_message(session, "yes", api=fake)
    assert session.quotes["Q-1"].rma_number == "RMA-1001-1"


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
