"""The orchestration loop: send history to Claude, run any tools it asks for, repeat until it
replies in plain text. This is the only place the LLM and the tools meet, so no guardrail can be
skipped by clever prompting."""

import json
import time
from functools import cache

import anthropic

from bookly.config import EFFORT, MAX_AGENT_STEPS, MAX_TOKENS, MODEL
from bookly.models import SessionState, TraceEvent
from bookly.prompt import build_system_prompt
from bookly.tools import TOOL_SCHEMAS, dispatch

# Said when the loop can't finish a reply, so the customer is never left with silence.
FALLBACK_REPLY = (
    "Sorry, I'm having trouble with this one. A teammate can follow up with you by email."
)


@cache
def client() -> anthropic.Anthropic:
    """Create the API client once (reads ANTHROPIC_API_KEY from the environment)."""
    return anthropic.Anthropic()


def handle_user_message(session: SessionState, text: str) -> str:
    """Take one customer message, run the LLM/tool loop, and return Dewey's reply."""
    session.turn_index += 1
    session.messages.append({"role": "user", "content": text})

    for _ in range(MAX_AGENT_STEPS):
        response = call_llm(session)
        # Append the full content (including thinking blocks), unchanged: history must stay append-only.
        session.messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "tool_use":
            session.messages.append({"role": "user", "content": run_tools(session, response)})
            continue
        if response.stop_reason == "refusal":
            return FALLBACK_REPLY
        return reply_text(response) or FALLBACK_REPLY

    return FALLBACK_REPLY  # step budget used up; slice 3 turns this into a real escalation ticket


def call_llm(session: SessionState) -> anthropic.types.Message:
    """Make one Messages API call with the full history and record it in the trace."""
    started = time.monotonic()
    response = client().messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=build_system_prompt(),
        tools=TOOL_SCHEMAS,
        messages=session.messages,
        output_config={"effort": EFFORT},
        cache_control={"type": "ephemeral"},  # reuse the unchanged prompt prefix across calls
    )
    session.trace.append(
        TraceEvent(
            turn=session.turn_index,
            kind="llm",
            name=MODEL,
            input={"message_count": len(session.messages)},
            output={
                "stop_reason": response.stop_reason,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_tokens": response.usage.cache_read_input_tokens,
            },
            ok=response.stop_reason != "refusal",
            latency_ms=elapsed_ms(started),
        )
    )
    return response


def run_tools(session: SessionState, response: anthropic.types.Message) -> list[dict]:
    """Run every tool call in the response and return their results, all in one user message."""
    results = []
    for block in response.content:
        if block.type != "tool_use":
            continue
        started = time.monotonic()
        output = dispatch(block.name, block.input, session)
        session.trace.append(
            TraceEvent(
                turn=session.turn_index,
                kind="tool",
                name=block.name,
                input=block.input,
                output=output,
                ok=output.get("ok", False),
                latency_ms=elapsed_ms(started),
            )
        )
        results.append(
            {
                "type": "tool_result",
                "tool_use_id": block.id,  # must match the tool_use block it answers
                "content": json.dumps(output),
                "is_error": not output.get("ok", False),
            }
        )
    return results


def reply_text(response: anthropic.types.Message) -> str:
    """Pull the customer-facing text out of a response (skipping thinking blocks)."""
    return "".join(block.text for block in response.content if block.type == "text").strip()


def elapsed_ms(started: float) -> int:
    """Milliseconds since a time.monotonic() reading."""
    return int((time.monotonic() - started) * 1000)
