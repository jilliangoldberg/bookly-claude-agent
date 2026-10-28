"""Runs one trial: the simulated customer talks to the real agent entry point, and presses the same
application buttons the Streamlit UI shows. Each trial starts from a fresh SessionState."""

import time
from dataclasses import dataclass, field

import anthropic

from bookly.actions import cancel_reply, cancel_return, confirm_reply, confirm_return, record_action_reply
from bookly.agent import handle_user_message
from bookly.models import SessionState
from evals.recording import CallRecord, ModelMismatch
from evals.scenarios import Scenario

NO_CARD = "There is no confirmation card on screen, so nothing happened."


@dataclass
class Trial:
    """Everything one simulated conversation produced, for grading and the report."""

    scenario_id: str
    n: int
    session: SessionState = field(default_factory=SessionState)
    events: list[dict] = field(default_factory=list)  # chronological: customer, dewey, tool, card, click, ...
    calls: list[CallRecord] = field(default_factory=list)
    status: str = "running"  # done | turn_limit | error
    error: str | None = None
    wall_ms: int = 0


def card_text(session: SessionState) -> str:
    """The confirmation card as plain text, from the canonical quote (mirrors app.render_confirmation)."""
    quote = session.quotes[session.pending_action.quote_id]
    lines = [
        f"Books: {', '.join(quote.item_titles)}",
        f"Order: {quote.order_id}",
        f"Resolution: {quote.resolution.replace('_', ' ')}",
        f"Refund amount: ${quote.refund_amount:.2f}",
    ]
    if quote.fee:
        lines.append(f"Return-label fee: ${quote.fee:.2f}")
    lines.append(f"Timing: {quote.timing}")
    return "\n".join(lines)


def run_trial(scenario: Scenario, n: int, agent_api, customer, calls: list[CallRecord] | None = None) -> Trial:
    """Play the scenario to the end. API failures end the trial with status "error" (never a model fail).
    `calls` is the list the RecordingClients append to, so the trial carries its own cost."""
    trial = Trial(scenario_id=scenario.id, n=n, calls=calls if calls is not None else [])
    session = trial.session
    started = time.monotonic()
    shown_card = None  # token of the card the customer last saw

    try:
        for _ in range(scenario.max_turns):
            move = customer.next_turn(scenario, trial.events)
            if move.action == "done":
                trial.status = "done"
                break

            if move.action == "say":
                trial.events.append({"kind": "customer", "text": move.text})
                traced = len(session.trace)
                reply = handle_user_message(session, move.text, api=agent_api)
                for e in session.trace[traced:]:
                    if e.kind == "tool":
                        trial.events.append({"kind": "tool", "turn": e.turn, "name": e.name,
                                             "input": e.input, "output": e.output, "ok": e.ok})
                trial.events.append({"kind": "dewey", "text": reply})
                if session.pending_action and session.pending_action.token != shown_card:
                    shown_card = session.pending_action.token
                    trial.events.append({"kind": "card", "token": shown_card, "text": card_text(session)})
                continue

            button = "Confirm return" if move.action == "click_confirm" else "Cancel"
            if session.pending_action is None:
                trial.events.append({"kind": "notice", "text": NO_CARD, "attempted": button})
                continue
            token = session.pending_action.token
            if move.action == "click_confirm":
                result = confirm_return(session, token)
                reply = confirm_reply(result)
            else:
                result = cancel_return(session, token)
                reply = cancel_reply(result)
            record_action_reply(session, reply)
            trial.events.append({"kind": "click", "button": button, "token": token})
            trial.events.append({"kind": "action_reply", "text": reply, "result": result})
        else:
            trial.status = "turn_limit"
    except (anthropic.APIError, ModelMismatch, RuntimeError) as exc:
        trial.status = "error"
        trial.error = f"{type(exc).__name__}: {exc}"

    trial.wall_ms = int((time.monotonic() - started) * 1000)
    return trial
