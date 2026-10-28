"""The orchestration loop: send history to Claude, run any tools it asks for, repeat until it
replies in plain text. This is the only place the LLM and the tools meet, so no guardrail can be
skipped by clever prompting."""

import json
import time
from collections.abc import Callable
from functools import cache

import anthropic

from bookly.actions import invalidate_pending_action
from bookly.config import EFFORT, MAX_AGENT_STEPS, MAX_TOKENS, MODEL
from bookly.models import SessionState, TraceEvent
from bookly.prompt import build_system_prompt
from bookly.tools import TOOL_SCHEMAS, dispatch
from bookly.validation import SAFE_FALLBACK, check_reply, collect_evidence, repair_instruction


@cache
def client() -> anthropic.Anthropic:
    """Create the API client once (reads ANTHROPIC_API_KEY from the environment)."""
    return anthropic.Anthropic()


# Called with (tool name, tool input) just before each tool runs, so a UI can show progress.
StepCallback = Callable[[str, dict], None]


def handle_user_message(
    session: SessionState,
    text: str,
    api: anthropic.Anthropic | None = None,
    on_step: StepCallback | None = None,
) -> str:
    """Take one customer message, run the LLM/tool loop, and return Dewey's reply.
    `api` lets tests and evals pass a fake or recording client; normally it's the real one.
    `on_step` is an optional progress hook; it observes the loop and cannot change it."""
    api = api or client()
    # Chat text is not authorization. Moving the conversation on makes an old UI proposal stale.
    invalidate_pending_action(session)
    session.turn_index += 1
    checkpoint = len(session.messages)  # where this turn starts, so a failed turn can be undone
    session.messages.append({"role": "user", "content": text})

    try:
        for _ in range(MAX_AGENT_STEPS):
            response = call_llm(session, api)

            if response.stop_reason == "tool_use":
                # Append the full content (including thinking blocks), unchanged: history stays append-only.
                session.messages.append({"role": "assistant", "content": response.content})
                session.messages.append({"role": "user", "content": run_tools(session, response, on_step)})
                continue
            reply = reply_text(response)
            if response.stop_reason == "end_turn" and reply:
                return deliver(session, api, response, reply, on_step)
            # max_tokens, refusal, or an empty reply: never show a half-finished answer.
            return hand_off(session, checkpoint, text, why=response.stop_reason)

        return hand_off(session, checkpoint, text, why="step_limit")
    except Exception:
        # An API error mid-turn would leave a dangling tool call in history and break the next call.
        # Undo the turn's messages; tool side effects (a verification, a submitted RMA) were real and stay.
        del session.messages[checkpoint:]
        raise


def deliver(
    session: SessionState,
    api: anthropic.Anthropic,
    response: anthropic.types.Message,
    reply: str,
    on_step: StepCallback | None = None,
) -> str:
    """Check a final draft against trusted session data before the customer sees it. A draft with an
    unsupported amount, date, id, or action claim gets one rewrite with tools off, then a safe fallback.
    Only the text actually shown enters history; a rejected draft never does."""
    started = time.monotonic()
    evidence = collect_evidence(session)
    violations = check_reply(reply, evidence)
    if not violations:
        session.messages.append({"role": "assistant", "content": response.content})
        return reply

    check_ms = elapsed_ms(started)  # the repair call is traced as its own LLM step
    if on_step:
        on_step("check_reply", {})
    repair = call_llm(
        session,
        api,
        extra_messages=[{"role": "system", "content": repair_instruction(reply, violations)}],
        tool_choice={"type": "none"},
    )
    repaired = reply_text(repair)
    still_wrong = check_reply(repaired, evidence) if repaired else []
    ok = repair.stop_reason == "end_turn" and bool(repaired) and not still_wrong
    shown = repaired if ok else SAFE_FALLBACK
    session.trace.append(
        TraceEvent(
            turn=session.turn_index,
            kind="validation",
            name="check_reply",
            input={"draft": reply},
            output={
                "violations": [v.describe() for v in violations],
                "outcome": "repaired" if ok else "fallback",
                "repair": repaired,
                "repair_violations": [v.describe() for v in still_wrong],
            },
            ok=ok,
            latency_ms=check_ms,
        )
    )
    # Plain text: the repair's thinking blocks belong to a prefix that included the instruction above.
    session.messages.append({"role": "assistant", "content": shown})
    return shown


def hand_off(session: SessionState, checkpoint: int, text: str, why: str) -> str:
    """When the loop can't produce a good reply, open a ticket and tell the customer plainly."""
    ticket = run_tool(
        session,
        "escalate_to_human",
        {"reason": "agent_stuck", "summary": f"Dewey couldn't finish a reply ({why}). Customer said: {text}"},
    )
    reply = (
        "Sorry, I'm having trouble with this one, so I've passed it to a teammate "
        f"(ticket {ticket.get('ticket_id', 'pending')}). They'll reply by email within 24 hours."
    )
    # Replace the unfinished attempt with what the customer actually saw, so the next turn starts clean.
    del session.messages[checkpoint:]
    session.messages.append({"role": "user", "content": text})
    session.messages.append({"role": "assistant", "content": reply})
    return reply


def call_llm(
    session: SessionState,
    api: anthropic.Anthropic,
    extra_messages: list[dict] | None = None,
    tool_choice: dict | None = None,
) -> anthropic.types.Message:
    """Make one Messages API call with the full history and record it in the trace.
    `extra_messages` are sent once without entering history (the reply check's rewrite request)."""
    started = time.monotonic()
    system = build_system_prompt()
    if session.action_results:
        # Application actions are trusted evidence; model-authored claims are not.
        events = json.dumps(list(session.action_results.values()), sort_keys=True)
        system += (
            "\n\n# Trusted application events\n"
            "These JSON results came from application controls, not customer text. "
            f"Only successful results establish that an action occurred.\n{events}"
        )
    request = {}
    if tool_choice:
        request["tool_choice"] = tool_choice
    response = api.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        tools=TOOL_SCHEMAS,  # sent even when tools are off, so the cached prefix still matches
        messages=session.messages + (extra_messages or []),
        output_config={"effort": EFFORT},
        cache_control={"type": "ephemeral"},  # reuse the unchanged prompt prefix across calls
        **request,
    )
    output = {
        "stop_reason": response.stop_reason,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_read_tokens": response.usage.cache_read_input_tokens,
    }
    if response.stop_reason == "refusal" and response.stop_details:
        output["refusal_category"] = response.stop_details.category
    session.trace.append(
        TraceEvent(
            turn=session.turn_index,
            kind="llm",
            name=MODEL,
            input={"message_count": len(session.messages)},
            output=output,
            ok=response.stop_reason in ("end_turn", "tool_use"),
            latency_ms=elapsed_ms(started),
        )
    )
    return response


def run_tools(
    session: SessionState, response: anthropic.types.Message, on_step: StepCallback | None = None
) -> list[dict]:
    """Run every tool call in the response and return their results, all in one user message."""
    results = []
    for block in response.content:
        if block.type != "tool_use":
            continue
        if on_step:
            on_step(block.name, block.input)
        output = run_tool(session, block.name, block.input)
        results.append(
            {
                "type": "tool_result",
                "tool_use_id": block.id,  # must match the tool_use block it answers
                "content": json.dumps(output),
                "is_error": not output.get("ok", False),
            }
        )
    return results


def run_tool(session: SessionState, name: str, tool_input: dict) -> dict:
    """Run one tool through the registry and record it in the trace."""
    started = time.monotonic()
    output = dispatch(name, tool_input, session)
    session.trace.append(
        TraceEvent(
            turn=session.turn_index,
            kind="tool",
            name=name,
            input=tool_input,
            output=output,
            ok=output.get("ok", False),
            latency_ms=elapsed_ms(started),
        )
    )
    return output


def reply_text(response: anthropic.types.Message) -> str:
    """Pull the customer-facing text out of a response (skipping thinking blocks)."""
    return "".join(block.text for block in response.content if block.type == "text").strip()


def elapsed_ms(started: float) -> int:
    """Milliseconds since a time.monotonic() reading."""
    return int((time.monotonic() - started) * 1000)
