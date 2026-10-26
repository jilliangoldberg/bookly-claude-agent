"""Streamlit UI: Dewey's chat on the left, a "glass box" sidebar on the right showing what code
decided (session state) and every LLM step and tool call (trace). UI only; all logic is in bookly/.
Run: streamlit run app.py"""

import json
from pathlib import Path

import anthropic
import streamlit as st

from bookly.actions import cancel_return, confirm_return
from bookly.agent import handle_user_message
from bookly.data import customers
from bookly.models import SessionState

DEWEY_AVATAR = str(Path(__file__).parent / "assets" / "dewey.png")
GREETING = "Hi, I'm Dewey, Bookly's bookworm! I can help with orders, returns, and store policies. What can I do for you?"

# One sample opener per demo customer, so a reviewer can start chatting in seconds.
SAMPLE_PROMPTS = {
    "C001": ["Where's my order?", "I'd like to return a book."],
    "C002": ["I want to return Northern Lines.", "Refund my ebook, The Quiet Engine."],
    "C003": ["Where's my order?", "My Cartographer's Daughter arrived with a torn cover."],
}

CSS = """
<style>
  h1, h2, h3 { font-family: Georgia, 'Times New Roman', serif; }
  [data-testid="stChatMessage"] { border-radius: 14px; padding: 0.6rem 0.9rem; }
  [data-testid="stSidebar"] code { font-size: 0.78rem; }
  .dewey-subtitle { color: #8a5a35; font-style: italic; margin-top: -0.6rem; }
</style>
"""


def escape_dollars(text: str) -> str:
    """Streamlit renders $...$ as math, which would mangle '$22.01'; escape it."""
    return text.replace("$", "\\$")


def new_conversation() -> None:
    """Start a fresh session: new agent state and an empty chat transcript."""
    st.session_state.agent_session = SessionState()
    st.session_state.chat = [("assistant", GREETING)]  # what the customer sees, kept apart from raw API history


def render_chat() -> None:
    """Draw every message in the transcript."""
    for role, text in st.session_state.chat:
        avatar = DEWEY_AVATAR if role == "assistant" else "🙂"
        st.chat_message(role, avatar=avatar).markdown(escape_dollars(text))


def handle_input(text: str) -> None:
    """Show the customer's message, run the agent, and show Dewey's reply."""
    st.session_state.chat.append(("user", text))
    st.chat_message("user", avatar="🙂").markdown(escape_dollars(text))

    with st.chat_message("assistant", avatar=DEWEY_AVATAR):
        with st.spinner("Dewey is flipping through the pages..."):
            try:
                reply = handle_user_message(st.session_state.agent_session, text)
            except anthropic.APIError as exc:
                reply = f"Sorry, I couldn't reach my bookshelf just now ({type(exc).__name__}). Please try again."
        st.markdown(escape_dollars(reply))
    st.session_state.chat.append(("assistant", reply))


def record_action_reply(result: dict, reply: str) -> None:
    """Record the deterministic result shown to the customer and future model turns."""
    session = st.session_state.agent_session
    session.messages.append({"role": "assistant", "content": reply})
    st.session_state.chat.append(("assistant", reply))


def confirm_pending_return(token: str) -> None:
    """Submit the exact proposal selected by the application's Confirm button."""
    result = confirm_return(st.session_state.agent_session, token)
    if result["ok"]:
        titles = ", ".join(result["item_titles"])
        reply = (
            f"Your return for **{titles}** has been submitted. "
            f"Your RMA is **{result['rma_number']}**. {result['next_steps']}"
        )
    else:
        reply = result["message"]
    record_action_reply(result, reply)


def cancel_pending_return(token: str) -> None:
    """Cancel the exact proposal selected by the application's Cancel button."""
    result = cancel_return(st.session_state.agent_session, token)
    reply = (
        "I've cancelled that return proposal. No return was submitted."
        if result["ok"]
        else result["message"]
    )
    record_action_reply(result, reply)


def render_confirmation(session: SessionState) -> None:
    """Render canonical proposal details with application-controlled actions."""
    pending = session.pending_action
    if pending is None:
        return
    quote = session.quotes[pending.quote_id]
    with st.container(border=True):
        st.subheader("Confirm this return")
        st.caption("The return is submitted only when you use the Confirm return button.")
        st.markdown(f"**Books:** {', '.join(quote.item_titles)}")
        st.markdown(f"**Order:** `{quote.order_id}`")
        st.markdown(f"**Resolution:** {quote.resolution.replace('_', ' ')}")
        st.markdown(f"**Refund amount:** {escape_dollars(f'${quote.refund_amount:.2f}')}")
        if quote.fee:
            st.markdown(f"**Return-label fee:** {escape_dollars(f'${quote.fee:.2f}')}")
        st.markdown(f"**Timing:** {quote.timing}")
        confirm, cancel = st.columns(2)
        confirm.button(
            "Confirm return",
            type="primary",
            key=f"confirm-{pending.token}",
            on_click=confirm_pending_return,
            args=(pending.token,),
            use_container_width=True,
        )
        cancel.button(
            "Cancel",
            key=f"cancel-{pending.token}",
            on_click=cancel_pending_return,
            args=(pending.token,),
            use_container_width=True,
        )


def render_demo_accounts() -> None:
    """Sidebar: the mock customers' login details plus sample things to say."""
    with st.expander("Demo accounts", expanded=False):
        for c in customers():
            st.markdown(f"**{c['first_name']} {c['last_name']}**  \n`{c['email']}` · ZIP `{c['zip_code']}`")
            for prompt in SAMPLE_PROMPTS.get(c["customer_id"], []):
                st.caption(f"Try: “{prompt}”")
        st.caption("No account needed: “Do you ship to Canada?” or “Can I sell you my used books?”")


def render_session_state(session: SessionState) -> None:
    """Sidebar: the facts code has committed to. The LLM can only change these by calling tools."""
    st.subheader("Session state")
    if session.verified_customer_id:
        st.markdown(f"✅ Verified: **{session.verified_first_name}** (`{session.verified_customer_id}`)")
    else:
        st.markdown(f"🔒 Not verified · failed attempts: {session.verify_attempts}")

    for quote in session.quotes.values():
        submitted = next(
            (
                result
                for result in session.action_results.values()
                if (
                    result.get("ok")
                    and result.get("status") == "submitted"
                    and result.get("quote_id") == quote.quote_id
                )
            ),
            None,
        )
        status = f"submitted · `{submitted['rma_number']}`" if submitted else "proposal only"
        st.markdown(
            f"🧾 Quote `{quote.quote_id}` · {quote.order_id} · {escape_dollars(f'${quote.refund_amount:.2f}')} "
            f"({quote.resolution.replace('_', ' ')}) · {status}"
        )
    if session.escalation:
        st.markdown(f"🙋 Ticket `{session.escalation['ticket_id']}` · {session.escalation['reason'].replace('_', ' ')}")


def render_trace(session: SessionState) -> None:
    """Sidebar: every LLM call and tool call, grouped by customer message, newest first."""
    st.subheader("Trace")
    if not session.trace:
        st.caption("Each LLM step and tool call will appear here.")
        return

    for turn in sorted({e.turn for e in session.trace}, reverse=True):
        events = [e for e in session.trace if e.turn == turn]
        total_s = sum(e.latency_ms for e in events) / 1000
        st.markdown(f"**Message {turn}** · {len(events)} steps · {total_s:.1f}s")
        for e in events:
            if e.kind == "llm":
                o = e.output
                st.caption(
                    f"🧠 {o['stop_reason']} · {o['input_tokens']}→{o['output_tokens']} tokens "
                    f"· cached {o['cache_read_tokens']} · {e.latency_ms} ms"
                )
            else:
                badge = "✅" if e.ok else f"⚠️ {e.output.get('error_code')}"
                with st.expander(f"🔧 {e.name} {badge} · {e.latency_ms} ms"):
                    st.markdown("Input")
                    st.code(json.dumps(e.input, indent=2), language="json")
                    st.markdown("Output")
                    st.code(json.dumps(e.output, indent=2), language="json")


# ---------------- Page ----------------
st.set_page_config(page_title="Dewey · Bookly Support", page_icon=DEWEY_AVATAR, layout="wide")
st.markdown(CSS, unsafe_allow_html=True)
if "agent_session" not in st.session_state:
    new_conversation()

header_img, header_text = st.columns([1, 10])
header_img.image(DEWEY_AVATAR, width=72)
header_text.title("Dewey")
header_text.markdown('<p class="dewey-subtitle">Bookly’s bookworm support assistant</p>', unsafe_allow_html=True)

render_chat()
render_confirmation(st.session_state.agent_session)
if text := st.chat_input("Ask Dewey about an order, a return, or a policy..."):
    handle_input(text)
    st.rerun()

# The sidebar is drawn last so it reflects whatever the message above just changed.
with st.sidebar:
    st.header("📖 Glass box")
    if st.button("Reset conversation", use_container_width=True):
        new_conversation()
        st.rerun()
    show_trace = st.toggle("Show trace", value=True)
    render_demo_accounts()
    render_session_state(st.session_state.agent_session)
    if show_trace:
        render_trace(st.session_state.agent_session)
