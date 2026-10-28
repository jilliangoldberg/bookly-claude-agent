"""Streamlit tests for the application-owned return confirmation boundary."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from bookly.tools import dispatch

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
MAYA = {"email": "maya.chen@example.com", "zip_code": "94110"}


def app_with_proposal() -> AppTest:
    """Open the real UI and seed a proposal through the same tools the model uses."""
    app = AppTest.from_file(APP_PATH).run(timeout=10)
    session = app.session_state["agent_session"]
    assert dispatch("verify_customer", MAYA, session)["ok"]
    assert dispatch(
        "prepare_return",
        {
            "order_id": "BK-1001",
            "item_ids": ["I-2"],
            "reason": "changed_mind",
            "resolution": "refund_original",
        },
        session,
    )["ok"]
    return app.run(timeout=10)


def test_confirmation_card_shows_canonical_quote():
    app = app_with_proposal()
    assert not app.exception
    page = " ".join(markdown.value for markdown in app.markdown)
    assert "Salt & Static" in page
    assert "BK-1001" in page
    assert "$22.01" in page
    assert any(button.label == "Confirm return" for button in app.button)
    assert any(button.label == "Cancel" for button in app.button)


def test_confirm_button_records_the_actual_result():
    app = app_with_proposal()
    next(button for button in app.button if button.label == "Confirm return").click().run(timeout=10)
    session = app.session_state["agent_session"]
    assert not app.exception
    assert session.pending_action is None
    assert len(session.action_results) == 1
    assert next(iter(session.action_results.values()))["status"] == "submitted"
    assert "RMA-1001-1" in app.chat_message[-1].markdown[0].value
    assert not any(button.label == "Confirm return" for button in app.button)


def test_loading_a_saved_scenario_fills_the_chat_and_session():
    app = AppTest.from_file(APP_PATH).run(timeout=10)
    app.session_state["view_mode"] = "Developer"
    app.run(timeout=10)
    next(b for b in app.button if b.key == "load-S3").click().run(timeout=10)
    assert not app.exception
    assert app.session_state["view_mode"] == "Customer"
    session = app.session_state["agent_session"]
    assert session.verified_customer_id == "C001"
    assert any(r.get("status") == "submitted" for r in session.action_results.values())
    page = " ".join(m.value for m in app.markdown)
    assert "Eval scenario S3" in page and "RMA-1001-1" in page

    next(b for b in app.button if b.label == "Reset conversation").click().run(timeout=10)
    assert app.session_state["loaded_scenario"] is None


def test_cancel_and_reset_do_not_submit():
    app = app_with_proposal()
    next(button for button in app.button if button.label == "Cancel").click().run(timeout=10)
    session = app.session_state["agent_session"]
    assert not app.exception
    assert session.pending_action is None
    assert next(iter(session.action_results.values()))["status"] == "cancelled"
    assert "No return was submitted" in app.chat_message[-1].markdown[0].value

    next(button for button in app.button if button.label == "Reset conversation").click().run(timeout=10)
    reset = app.session_state["agent_session"]
    assert reset.pending_action is None
    assert reset.action_results == {}
    assert len(app.session_state["chat"]) == 1
