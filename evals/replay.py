"""Rebuild a live session from a recorded eval transcript, so a reviewer can watch a scenario in the
real UI (and keep chatting from where it ended) without calling the API.

Tool calls and button presses are re-run through the real code, in order; they are deterministic, so
the session state matches the recording. Any result that no longer matches is reported as drift
(the data, policy, or code changed since the transcript was saved). Model replies are replayed as text."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from bookly.actions import (
    cancel_reply,
    cancel_return,
    confirm_reply,
    confirm_return,
    invalidate_pending_action,
    record_action_reply,
)
from bookly.models import SessionState, TraceEvent
from bookly.tools import dispatch

SHOWCASE_DIR = Path(__file__).resolve().parent / "showcase"


@dataclass
class Replay:
    """A session rebuilt from a transcript, plus what the customer saw."""

    session: SessionState
    chat: list[tuple[str, str]]  # (role, text), the UI's transcript format
    drift: list[str] = field(default_factory=list)


def replay(record: dict) -> Replay:
    """Re-run a saved trial's events into a fresh SessionState."""
    session = SessionState()
    chat: list[tuple[str, str]] = []
    drift: list[str] = []

    for e in record["events"]:
        kind = e["kind"]
        if kind == "customer":  # what handle_user_message does before the model runs
            invalidate_pending_action(session)
            session.turn_index += 1
            session.messages.append({"role": "user", "content": e["text"]})
            chat.append(("user", e["text"]))
        elif kind == "tool":
            output = dispatch(e["name"], e["input"], session)
            session.trace.append(TraceEvent(turn=session.turn_index, kind="tool", name=e["name"], input=e["input"],
                                            output=output, ok=output.get("ok", False), latency_ms=0))
            if json.dumps(output, sort_keys=True, default=str) != json.dumps(e["output"], sort_keys=True, default=str):
                drift.append(f"{e['name']} on message {session.turn_index} returned something different now")
        elif kind == "dewey":
            session.messages.append({"role": "assistant", "content": e["text"]})
            chat.append(("assistant", e["text"]))
        elif kind == "click":
            if session.pending_action is None or session.pending_action.token != e["token"]:
                drift.append(f"{e['button']} pressed, but proposal {e['token']} isn't on screen now")
                continue
            if e["button"] == "Confirm return":
                reply = confirm_reply(confirm_return(session, e["token"]))
            else:
                reply = cancel_reply(cancel_return(session, e["token"]))
            record_action_reply(session, reply)
            chat.append(("assistant", reply))
        # "card", "notice", and "action_reply" need no replay: the card is drawn from session state,
        # and the button's reply was just rebuilt from the real action result.
    return Replay(session, chat, drift)


def load_showcase(directory: Path = SHOWCASE_DIR) -> dict[str, dict]:
    """The committed example transcript for each scenario, keyed by scenario id."""
    records = {}
    for path in sorted(directory.glob("*.json")):
        record = json.loads(path.read_text())
        records[record["scenario"]] = record
    return records
