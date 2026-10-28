"""Streamlit UI: a Customer view with Dewey's chat in a small window, and a Developer "glass box"
view showing what code decided (session state) and every LLM step and tool call (trace).
UI only; all logic is in bookly/. Run: streamlit run app.py"""

import base64
import json
import random
from pathlib import Path
from urllib.parse import quote

import anthropic
import streamlit as st

from bookly.actions import cancel_reply, cancel_return, confirm_reply, confirm_return, record_action_reply
from bookly.agent import handle_user_message
from bookly.data import customers
from bookly.models import SessionState
from evals.gates import run_gates
from evals.recording import RecordingClient
from evals.replay import load_showcase, replay
from evals.report import Graded, decide, trial_record
from evals.runner import run_trial
from evals.scenarios import SCENARIOS, select
from evals.simulator import SimulatedCustomer

DEWEY_AVATAR = str(Path(__file__).parent / "assets" / "dewey.png")
GREETING = "Hi, I'm Dewey, Bookly's bookworm! I can help with orders, returns, and store policies. What can I do for you?"

# One sample opener per demo customer, so a reviewer can start chatting in seconds.
SAMPLE_PROMPTS = {
    "C001": ["Where's my order?", "I'd like to return a book."],
    "C002": ["I want to return Northern Lines.", "Refund my ebook, The Quiet Engine."],
    "C003": ["Where's my order?", "My Cartographer's Daughter arrived with a torn cover."],
}

# Openers shown under the greeting before the customer has typed anything.
QUICK_PROMPTS = ["Where's my order?", "Start a return", "Do you ship to Canada?"]

# While Dewey works: plain typing dots, a line naming the tool step, or (only on a slow turn
# with no tool yet) one of these, faded in after a few seconds.
TOOL_STATUS = {
    "verify_customer": "Checking your account…",
    "list_orders": "Looking through your orders…",
    "get_order": "Pulling up your order…",
    "prepare_return": "Working out your refund…",
    "search_policies": "Checking store policy…",
    "escalate_to_human": "Passing this to a teammate…",
    "check_reply": "Double-checking the details…",  # the reply check is rewriting a draft
}
SLOW_TURN_LINES = ["Flipping through the pages…", "Still reading…", "Checking the shelves…"]

# Small "chat window" layout: a story column on the left, a fixed-height chat card on the right.
CSS = """
<style>
  :root { --ink: #3b2f2a; --muted: #8b7d70; --paper: #fffdf8; --leather: #8a5a35;
          --line: #eadfcd; --dewey: #eef1e4; --you: #f8e3cc; }
  header[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stSidebar"],
  [data-testid="stSidebarCollapsedControl"] { display: none; }
  .block-container { max-width: 1080px; padding-top: 2rem; padding-bottom: 1rem; }
  h1, h2, h3, h4 { font-family: Georgia, 'Times New Roman', serif; color: var(--ink); }

  .brand { display: flex; align-items: center; gap: 12px; }
  .brand img { width: 40px; height: 40px; border-radius: 10px; background: #f6ead6; }
  .brand h1 { font-size: 1.15rem; margin: 0; padding: 0; }
  .brand p, .eyebrow, .scene-caption, .chat-date, .site-footer {
    margin: 0; color: var(--muted); font: 11px/1.4 "Courier New", monospace;
    letter-spacing: 1.6px; text-transform: uppercase; }
  .story .eyebrow { margin-bottom: 0.4rem; }
  .story h2 { font-size: 2.6rem; line-height: 1.12; letter-spacing: -1px; margin: 0.6rem 0; padding: 0; }
  .story .intro { color: var(--muted); max-width: 340px; font-size: 0.95rem; }
  .story img { display: block; width: 190px; margin: 1.6rem auto 0.4rem; }
  .scene-caption { text-align: center; text-transform: none; }
  .site-footer { display: flex; justify-content: space-between; margin-top: 2.5rem; }

  .st-key-chat-window { background: var(--paper); border-radius: 16px !important;
    border-color: var(--line) !important; box-shadow: 0 10px 40px #6b553f12; }  .desk { display: flex; align-items: center; gap: 12px; padding-bottom: 12px;
    border-bottom: 1px solid var(--line); }
  .desk img { width: 38px; height: 38px; border-radius: 10px; background: #f6ead6; }
  .desk h4 { font-size: 0.95rem; margin: 0; padding: 0; }
  .desk p { margin: 0; color: var(--muted); font-size: 0.75rem; }
  .desk .dot { display: inline-block; width: 6px; height: 6px; background: #7c9b78; margin-right: 5px; }
  .desk .no { margin-left: auto; color: #b3a796; font: 10px "Courier New", monospace; }
  .chat-date { text-align: center; padding: 18px 0 14px; }
  .pills { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin: -0.3rem 0 0.8rem; }
  .pill { padding: 2px 10px; border-radius: 999px; font-size: 0.75rem; border: 1px solid; }
  .pill.ok { color: #4f6b4a; border-color: #b9c9b0; background: #eef1e4; }
  .pill.bad { color: #9a3b2a; border-color: #e3b5a8; background: #fbe9e3; }
  .pills .source { margin-left: auto; color: var(--muted); font-size: 0.75rem; }
  /* Scenario list: compact, left-aligned rows that wrap long titles instead of "…". */
  .st-key-scenario-list { gap: 0.4rem; }
  [class*="st-key-load-"] button { justify-content: flex-start; min-height: 2.6rem; padding: 0.45rem 0.8rem;
    font-size: 0.85rem; border-color: var(--line); }
  [class*="st-key-load-"] button div, [class*="st-key-load-"] button span {
    justify-content: flex-start !important; text-align: left !important; }
  [class*="st-key-load-"] button * { white-space: normal !important; overflow: visible !important;
    text-overflow: clip !important; }
  .empty-panel { padding: 2.2rem 1.6rem; border: 1px dashed var(--line); border-radius: 16px;
    background: var(--paper); color: var(--muted); font-size: 0.95rem; }
  /* "Behind the scenes" under the customer chat: smaller headings than the page's own. */
  .st-key-behind-scenes h3 { font-size: 1rem; margin-top: 0.4rem; }
  .replay-note { margin: 10px 0 0; padding: 6px 10px; border-radius: 8px; background: #f6ead6;
    color: var(--leather); font-size: 0.75rem; text-align: center; }

  .st-key-chat-log [data-testid="stChatMessage"] { padding: 0.55rem 0.85rem; border-radius: 12px;
    background: var(--dewey); max-width: 90%; font-size: 0.92rem; gap: 0.6rem; }
  .st-key-chat-log [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
    background: var(--you); margin-left: auto; width: fit-content; flex-direction: row-reverse; }
  .st-key-chat-log [data-testid="stVerticalBlockBorderWrapper"] { font-size: 0.88rem; }
  .st-key-chat-log [data-testid="stChatMessageAvatarUser"] { display: none; }
  [data-testid="stChatMessage"] p { margin-bottom: 0.4rem; }
  [data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] { margin-bottom: 0; }
  [data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] > :last-child { margin-bottom: 0; }

  .typing { display: flex; align-items: center; gap: 10px; min-height: 1.6rem; color: var(--muted); }
  .typing .dots { display: inline-flex; gap: 4px; }
  .typing .dots i { width: 6px; height: 6px; border-radius: 50%; background: #9aa58c;
    animation: bob 1.2s infinite ease-in-out; }
  .typing .dots i:nth-child(2) { animation-delay: 0.15s; }
  .typing .dots i:nth-child(3) { animation-delay: 0.3s; }
  .typing-line { font-size: 0.85rem; font-style: italic; animation: fade-in 0.3s both; }
  .typing-line.slow { animation: fade-in 0.6s 4s both; }
  @keyframes bob { 0%, 60%, 100% { transform: translateY(0); opacity: 0.5; }
                   30% { transform: translateY(-4px); opacity: 1; } }
  @keyframes fade-in { from { opacity: 0; } to { opacity: 1; } }

  .st-key-quick-prompts button, .st-key-new-chat button { font-size: 0.75rem; min-height: 0;
    padding: 0.2rem 0.55rem; border-color: var(--line); color: var(--muted); }
  .st-key-new-chat button { border: 0; background: transparent; text-decoration: underline; }
  .st-key-chat-window [data-testid="stChatInput"] { border-color: var(--line); }
  [data-testid="stSegmentedControl"] { justify-content: flex-end; }
</style>
"""


def escape_dollars(text: str) -> str:
    """Streamlit renders $...$ as math, which would mangle '$22.01'; escape it."""
    return text.replace("$", "\\$")


def new_conversation() -> None:
    """Start a fresh session: new agent state and an empty chat transcript."""
    st.session_state.agent_session = SessionState()
    st.session_state.chat = [("assistant", GREETING)]  # what the customer sees, kept apart from raw API history
    st.session_state.loaded_scenario = None  # an eval record shown in the chat, if one was loaded


def show_scenario(record: dict, session: SessionState, chat: list[tuple[str, str]], drift: list[str]) -> None:
    """Make a scenario the current conversation: shown in the Developer view, and continuable as the customer."""
    st.session_state.agent_session = session
    st.session_state.chat = [("assistant", GREETING), *chat]
    st.session_state.loaded_scenario = {**record, "drift": drift}


def load_saved_scenario(scenario_id: str) -> None:
    """Replay a committed example run: free, instant, and the session is real, so chatting can continue."""
    record = load_showcase()[scenario_id]
    replayed = replay(record)
    show_scenario(record, replayed.session, replayed.chat, replayed.drift)


def run_live_scenario(scenario_id: str) -> None:
    """Play a fresh simulated conversation against the live model, graded by the deterministic gates only."""
    [scenario] = select([scenario_id])
    client = anthropic.Anthropic()
    calls = []
    trial = run_trial(scenario, 1, agent_api=RecordingClient(client, "agent", calls),
                      customer=SimulatedCustomer(RecordingClient(client, "customer", calls)), calls=calls)
    if trial.status == "error":
        st.session_state.live_error = f"{scenario_id} couldn't run: {trial.error}"
        return
    gates = run_gates(scenario, trial)
    graded = Graded(scenario, trial, gates, verdicts=None)
    graded.verdict, graded.flags = decide(scenario, gates, None)
    chat = [("user" if e["kind"] == "customer" else "assistant", e["text"])
            for e in trial.events if e["kind"] in ("customer", "dewey", "action_reply")]
    show_scenario({**trial_record(graded), "live": True}, trial.session, chat, [])


def typing_html(line: str | None = None) -> str:
    """Animated dots, plus either what Dewey is doing now or a fallback that fades in on slow turns.
    Lines are fixed per tool: model-supplied values (order IDs, emails) are never echoed here."""
    if line:
        text = f'<span class="typing-line">{line}</span>'
    else:
        text = f'<span class="typing-line slow">{random.choice(SLOW_TURN_LINES)}</span>'
    return f'<div class="typing"><span class="dots"><i></i><i></i><i></i></span>{text}</div>'


def render_chat() -> None:
    """Draw every message in the transcript."""
    for role, text in st.session_state.chat:
        avatar = DEWEY_AVATAR if role == "assistant" else None
        st.chat_message(role, avatar=avatar).markdown(escape_dollars(text))


def handle_input(text: str) -> None:
    """Show the customer's message, run the agent, and show Dewey's reply."""
    st.session_state.chat.append(("user", text))
    st.chat_message("user").markdown(escape_dollars(text))

    with st.chat_message("assistant", avatar=DEWEY_AVATAR):
        status = st.empty()
        status.markdown(typing_html(), unsafe_allow_html=True)

        def show_step(tool_name: str, _tool_input: dict) -> None:
            if line := TOOL_STATUS.get(tool_name):
                status.markdown(typing_html(line), unsafe_allow_html=True)

        try:
            reply = handle_user_message(st.session_state.agent_session, text, on_step=show_step)
        except anthropic.APIError as exc:
            reply = f"Sorry, I couldn't reach my bookshelf just now ({type(exc).__name__}). Please try again."
        status.markdown(escape_dollars(reply))
    st.session_state.chat.append(("assistant", reply))


def show_action_reply(reply: str) -> None:
    """Record the deterministic result shown to the customer and future model turns."""
    record_action_reply(st.session_state.agent_session, reply)
    st.session_state.chat.append(("assistant", reply))


def confirm_pending_return(token: str) -> None:
    """Submit the exact proposal selected by the application's Confirm button."""
    show_action_reply(confirm_reply(confirm_return(st.session_state.agent_session, token)))


def cancel_pending_return(token: str) -> None:
    """Cancel the exact proposal selected by the application's Cancel button."""
    show_action_reply(cancel_reply(cancel_return(st.session_state.agent_session, token)))


def render_confirmation(session: SessionState) -> None:
    """Render canonical proposal details with application-controlled actions."""
    pending = session.pending_action
    if pending is None:
        return
    quote = session.quotes[pending.quote_id]
    with st.container(border=True):
        st.markdown("#### Confirm this return")
        st.caption("The return is submitted only when you use the Confirm return button.")
        lines = [
            f"**Books:** {', '.join(quote.item_titles)}",
            f"**Order:** `{quote.order_id}`",
            f"**Resolution:** {quote.resolution.replace('_', ' ')}",
            f"**Refund amount:** {escape_dollars(f'${quote.refund_amount:.2f}')}",
        ]
        if quote.fee:
            lines.append(f"**Return-label fee:** {escape_dollars(f'${quote.fee:.2f}')}")
        lines.append(f"**Timing:** {quote.timing}")
        st.markdown("  \n".join(lines))
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
            elif e.kind == "validation":
                o = e.output
                badge = "rewritten" if o["outcome"] == "repaired" else "safe fallback sent"
                with st.expander(f"🛡️ Reply check · {len(o['violations'])} unsupported · {badge}"):
                    st.markdown("Unsupported in the draft")
                    st.code("\n".join(o["violations"]), language="text")
                    st.markdown("Rejected draft (never shown to the customer)")
                    st.code(e.input["draft"], language="text")
                    if o["repair_violations"]:
                        st.markdown("Still unsupported after the rewrite")
                        st.code("\n".join(o["repair_violations"]), language="text")
            else:
                badge = "✅" if e.ok else f"⚠️ {e.output.get('error_code')}"
                with st.expander(f"🔧 {e.name} {badge} · {e.latency_ms} ms"):
                    st.markdown("Input")
                    st.code(json.dumps(e.input, indent=2), language="json")
                    st.markdown("Output")
                    st.code(json.dumps(e.output, indent=2), language="json")


def queue_prompt(text: str) -> None:
    """Quick-prompt buttons hand their text to the next run, as if it were typed."""
    st.session_state.queued_prompt = text


def render_customer_view() -> None:
    """A story column beside a small, fixed-height chat window."""
    story, window = st.columns([1, 1.3], gap="large", vertical_alignment="center")
    story.markdown(
        f"""<div class="story">
          <p class="eyebrow">Customer support</p>
          <h2>Welcome to Bookly!</h2>
          <p class="intro">Dewey can track an order, start a return, or answer a store policy question.</p>
          <img src="{DEWEY_DATA_URI}" alt="Dewey the bookworm">
          <p class="scene-caption">every book finds its reader.</p>
        </div>""",
        unsafe_allow_html=True,
    )

    with window.container(border=True, key="chat-window"):
        st.markdown(
            f"""<div class="desk">
              <img src="{DEWEY_DATA_URI}" alt="">
              <div><h4>Dewey</h4><p><span class="dot"></span>Bookly’s bookworm, here to help</p></div>
              <span class="no">No. 001</span>
            </div>""",
            unsafe_allow_html=True,
        )
        if loaded := st.session_state.loaded_scenario:
            source = "live run" if loaded.get("live") else "saved eval run"
            st.markdown(
                f'<p class="replay-note">Eval scenario {loaded["scenario"]} · {source} · '
                f'{loaded["verdict"]}. Grades are in the Developer view.</p>',
                unsafe_allow_html=True,
            )
        log = st.container(height=380, border=False, key="chat-log")
        with log:
            st.markdown('<p class="chat-date">You’ve opened a book</p>', unsafe_allow_html=True)
            render_chat()
            render_confirmation(st.session_state.agent_session)

        if len(st.session_state.chat) == 1:
            with st.container(horizontal=True, key="quick-prompts"):
                for prompt in QUICK_PROMPTS:
                    st.button(prompt, on_click=queue_prompt, args=(prompt,))

        typed = st.chat_input("Ask Dewey about an order, a return, or a policy...")
        if text := typed or st.session_state.pop("queued_prompt", None):
            with log:
                handle_input(text)
            st.rerun()

        with st.container(horizontal=True, horizontal_alignment="right", key="new-chat"):
            if st.button("Reset conversation"):
                new_conversation()
                st.rerun()

    # Behind the scenes, lined up under the chat: demo logins on the left, Dewey's state and steps on the right.
    accounts, behind = st.columns([1, 1.3], gap="large")
    with accounts:
        render_demo_accounts()
    with behind.container(key="behind-scenes"):
        st.markdown('<p class="eyebrow">Behind the scenes</p>', unsafe_allow_html=True)
        render_session_state(st.session_state.agent_session)
        render_trace(st.session_state.agent_session)


VERDICT_MARK = {"pass": "✅", "fail": "❌", "invalid": "➖", "unknown": "❔"}


def render_scenario_list() -> None:
    """Left column: every eval scenario. Clicking one replays its saved run into the panel on the right."""
    saved = load_showcase()
    loaded = st.session_state.loaded_scenario
    st.markdown("#### Eval scenarios")
    with st.container(key="scenario-list"):
        for scenario in SCENARIOS:
            record = saved.get(scenario.id)
            mark = VERDICT_MARK[record["verdict"]] if record else "·"
            st.button(
                f"{mark} {scenario.id} · {scenario.title}",
                key=f"load-{scenario.id}",
                help=scenario.rationale,
                disabled=record is None,
                type="primary" if loaded and loaded["scenario"] == scenario.id else "secondary",
                on_click=load_saved_scenario,
                args=(scenario.id,),
                use_container_width=True,
            )
    st.caption("Saved runs replay through the real tools, so you can keep chatting from the Customer view.")
    if loaded and st.button(f"Run {loaded['scenario']} live", key="run-live", use_container_width=True,
                            help="A fresh simulated customer against the model: about $0.04 and 30 seconds, "
                                 "graded by the deterministic checks only."):
        st.session_state.live_run = loaded["scenario"]
        st.rerun()


def render_scenario_panel() -> None:
    """Right column: the conversation as the customer saw it, then how it was graded."""
    record = st.session_state.loaded_scenario
    if record:
        [scenario] = select([record["scenario"]])
        source = "live run" if record.get("live") else "saved run"
        st.markdown(f"#### {scenario.id}: {scenario.title} {VERDICT_MARK[record['verdict']]}")
        passed = sum(g["ok"] for g in record["gates"])
        pills = [f'<span class="pill {"ok" if passed == len(record["gates"]) else "bad"}">'
                 f'checks {passed}/{len(record["gates"])}</span>']
        pills += [f'<span class="pill {"ok" if v["verdict"] == "pass" else "bad"}">{d.replace("_", " ")}</span>'
                  for d, v in record.get("judge", {}).items()]
        st.markdown(f'<div class="pills">{"".join(pills)}<span class="source">{source}</span></div>',
                    unsafe_allow_html=True)
        for warning in record.get("drift", []):
            st.warning(f"Replay drift: {warning}. The saved run may be out of date; re-run the evals.")
    else:
        st.markdown("#### Pick a scenario")
        st.markdown(
            '<div class="empty-panel">Choose an eval scenario on the left to see the whole conversation, '
            "how it was graded, and the judge's reasoning.</div>",
            unsafe_allow_html=True,
        )
        return

    with st.container(border=True, key="chat-window"):
        with st.container(height=420, border=False, key="chat-log"):
            render_chat()

    tools = [e for e in record["events"] if e["kind"] == "tool"]
    if tools:
        steps = " → ".join(f"{e['name']} {'✓' if e['ok'] else '✗'}" for e in tools)
        st.caption(f"Tools: {steps}. The full trace is under the chat on the Customer tab.")
    with st.expander(f"Deterministic checks · {passed}/{len(record['gates'])} passed"):
        for gate in record["gates"]:
            st.markdown(f"{'✅' if gate['ok'] else '❌'} `{gate['name']}` {gate['detail']}")
    for dimension, verdict in record.get("judge", {}).items():
        with st.expander(f"{VERDICT_MARK[verdict['verdict']]} Judge · {dimension.replace('_', ' ')}"):
            st.markdown(escape_dollars(verdict["reasoning"]))


def render_developer_view() -> None:
    """Eval scenarios beside the selected conversation and its grades (the live chat's trace is on the Customer tab)."""
    if error := st.session_state.pop("live_error", None):
        st.error(error)
    scenarios, panel = st.columns([1.15, 2.2], gap="large")
    with scenarios:
        render_scenario_list()
    with panel:
        render_scenario_panel()


# ---------------- Page ----------------
st.set_page_config(page_title="Dewey · Bookly Support", page_icon=DEWEY_AVATAR, layout="wide")
st.markdown(CSS, unsafe_allow_html=True)
# Faint line-art books tiled behind the page; embedded so the background needs no image requests.
BOOK_PATTERN = quote((Path(__file__).parent / "assets" / "book-pattern.svg").read_text())
st.markdown(
    f'<style>.stApp {{ background-image: url("data:image/svg+xml,{BOOK_PATTERN}"); '
    "background-size: 560px 440px; }</style>",
    unsafe_allow_html=True,
)
DEWEY_DATA_URI = "data:image/png;base64," + base64.b64encode(Path(DEWEY_AVATAR).read_bytes()).decode()
if "agent_session" not in st.session_state:
    new_conversation()
if live_id := st.session_state.pop("live_run", None):  # before the view toggle exists, so we can switch it
    with st.spinner(f"Running {live_id} live: a simulated customer is talking to Dewey…"):
        run_live_scenario(live_id)

brand, mode_col = st.columns([3, 2], vertical_alignment="center")
brand.markdown(
    f"""<div class="brand"><img src="{DEWEY_DATA_URI}" alt="">
      <div><h1>Bookly</h1><p>Stories, delivered</p></div></div>""",
    unsafe_allow_html=True,
)
mode = mode_col.segmented_control(
    "View", ["Customer", "Developer"], default="Customer", key="view_mode", label_visibility="collapsed"
)

if mode == "Developer":
    render_developer_view()
else:
    render_customer_view()

st.markdown(
    '<div class="site-footer"><span>Bookly · Dewey support</span><span>📖</span></div>',
    unsafe_allow_html=True,
)
