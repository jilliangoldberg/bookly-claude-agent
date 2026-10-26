"""Tiny dev chat over the same agent the UI uses, for testing prompts without Streamlit.
Run: python -m bookly.cli   (type 'reset' for a new session, 'quit' to exit)"""

import json
import os

import anthropic

from bookly.agent import handle_user_message
from bookly.models import SessionState

DIM, RESET = "\033[2m", "\033[0m"  # terminal colors, so tool calls fade behind the conversation


def print_new_tool_calls(session: SessionState, already_shown: int) -> None:
    """Print, dimmed, each tool call the agent made since the last reply."""
    for event in session.trace[already_shown:]:
        if event.kind == "tool":
            status = "ok" if event.ok else event.output.get("error_code")
            print(f"{DIM}  [tool] {event.name}({json.dumps(event.input)}) -> {status}{RESET}")


def main() -> None:
    """Read customer messages from the terminal and print Dewey's replies."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY in .env first (see .env.example).")
        return

    session = SessionState()
    print("Dewey: Hi, I'm Dewey, Bookly's bookworm! I can help with orders, returns, and store policies. What can I do for you?")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text == "quit":
            break
        if text == "reset":
            session = SessionState()
            print("(new session)")
            continue
        if not text:
            continue

        shown = len(session.trace)
        try:
            reply = handle_user_message(session, text)
        except anthropic.APIError as exc:
            print(f"(API error: {exc})")
            continue
        print_new_tool_calls(session, shown)
        print(f"\nDewey: {reply}")
        if session.pending_action:
            print(f"{DIM}(Return proposal {session.pending_action.token} is ready; confirm or cancel it in Streamlit.){RESET}")


if __name__ == "__main__":
    main()
