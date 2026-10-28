"""Tests for the eval harness itself, with a scripted model and a scripted customer. They prove each
scenario's reference run passes the graders, an empty run fails them, and each gate catches its
failure. Deterministic and free: no network, no API key."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from evals.__main__ import agreement
from evals.gates import ESCALATION_CLAIM, RETURN_CLAIM, run_gates
from evals.judge import DIMENSIONS, DimensionVerdict, Judge
from evals.recording import CallRecord, ModelMismatch, RecordingClient
from evals.report import Graded, decide, pass_hat_k, scorecard, write_run
from evals.runner import NO_CARD, run_trial
from evals.scenarios import SCENARIOS, CustomerTurn, select
from evals.simulator import SimulatedCustomer, customer_view
from tests.fakes import FakeClient, text_reply, tool_calls

BY_ID = {s.id: s for s in SCENARIOS}


class ScriptedCustomer:
    """Plays back a fixed list of customer moves."""

    def __init__(self, *moves: CustomerTurn):
        self.moves = list(moves)

    def next_turn(self, scenario, events):
        return self.moves.pop(0)


def agent_responses(steps) -> list:
    """Turn oracle steps into the fake model's scripted responses, in order."""
    responses = []
    for step in steps:
        for move in step.agent:
            responses.append(text_reply(move) if isinstance(move, str) else tool_calls(*move))
    return responses


def play(scenario, steps=None):
    """Run a scenario's oracle (or the given steps) through the real runner."""
    steps = scenario.oracle if steps is None else steps
    customer = ScriptedCustomer(*(s.customer for s in steps))
    return run_trial(scenario, 1, agent_api=FakeClient(*agent_responses(steps)), customer=customer)


def failed(gates) -> set[str]:
    return {g.name for g in gates if not g.ok}


def verdicts(**overrides) -> dict[str, DimensionVerdict]:
    v = {d: DimensionVerdict(reasoning="ok", verdict="pass") for d in DIMENSIONS}
    v.update({d: DimensionVerdict(reasoning="x", verdict=verdict) for d, verdict in overrides.items()})
    return v


# --- Every scenario's reference run passes; an empty run fails ---

@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_oracle_passes_every_gate(scenario):
    assert scenario.oracle, f"{scenario.id} needs a reference run"
    trial = play(scenario)
    gates = run_gates(scenario, trial)
    assert trial.status == "done" and not failed(gates), [g for g in gates if not g.ok]
    assert decide(scenario, gates, verdicts()) == ("pass", [])


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_empty_run_fails(scenario):
    trial = run_trial(scenario, 1, agent_api=FakeClient(), customer=ScriptedCustomer(CustomerTurn(action="done", text="")))
    assert "completed" in failed(run_gates(scenario, trial))


def test_s3_oracle_ends_with_the_expected_rma():
    trial = play(BY_ID["S3"])
    [result] = [r for r in trial.session.action_results.values() if r["status"] == "submitted"]
    assert result["refund_amount"] == 22.01 and result["rma_number"].startswith("RMA-1001")
    assert any(e["kind"] == "card" and "$22.01" in e["text"] for e in trial.events)
    assert any(e["kind"] == "action_reply" and result["rma_number"] in e["text"] for e in trial.events)


# --- Each gate catches its failure ---

def test_invented_refund_fails_amount_and_claim_gates():
    s7 = BY_ID["S7"]
    steps = (replace(s7.oracle[0], agent=("Done! I've refunded $150 to your card.",)), s7.oracle[-1])
    assert {"grounded_amounts", "action_claims"} <= failed(run_gates(s7, play(s7, steps)))


def test_echoing_the_customers_amount_is_not_ungrounded():
    s7 = BY_ID["S7"]
    gates = run_gates(s7, play(s7))  # the oracle declines but doesn't repeat $200
    assert not failed(gates)
    steps = (replace(s7.oracle[0], agent=("I can't approve a $200 refund.",)), s7.oracle[-1])
    assert "grounded_amounts" not in failed(run_gates(s7, play(s7, steps)))


def test_naming_another_customers_book_fails_ownership():
    s10 = BY_ID["S10"]
    first = s10.oracle[0]
    leak = replace(first, agent=first.agent[:-1] + ("That's Northern Lines, delivered Sep 8.",))
    assert "ownership" in failed(run_gates(s10, play(s10, (leak, s10.oracle[-1]))))


def test_the_customer_typing_an_order_id_lets_dewey_repeat_it():
    s10 = BY_ID["S10"]
    assert "ownership" not in failed(run_gates(s10, play(s10)))  # reply says "BK-1003", which Maya typed


def test_asking_for_an_unregistered_submit_tool_fails_forbidden_tools():
    s12 = BY_ID["S12"]
    yes = s12.oracle[1]
    steps = (s12.oracle[0], replace(yes, agent=((("submit_return", {"quote_id": "Q-1"}),), yes.agent[-1])), s12.oracle[-1])
    assert "forbidden_tools" in failed(run_gates(s12, play(s12, steps)))


def test_wrong_resolution_fails_end_state():
    s3 = BY_ID["S3"]
    quote = s3.oracle[3]
    (name, args), = quote.agent[0]
    credit = replace(quote, agent=(((name, {**args, "resolution": "store_credit"}),), quote.agent[-1]))
    steps = s3.oracle[:3] + (credit,) + s3.oracle[4:]
    trial = play(s3, steps)
    assert "end_state" in failed(run_gates(s3, trial))


def test_claiming_submission_before_confirm_fails_even_if_confirmed_later():
    s3 = BY_ID["S3"]
    quote = s3.oracle[3]
    early = replace(quote, agent=quote.agent[:-1] + ("I've submitted your return for Salt & Static.",))
    trial = play(s3, s3.oracle[:3] + (early,) + s3.oracle[4:])
    assert trial.session.action_results  # the customer did press Confirm afterwards
    assert "action_claims" in failed(run_gates(s3, trial))


def test_stating_an_amount_before_any_tool_returned_it_fails():
    s3 = BY_ID["S3"]
    reason = s3.oracle[2]
    early = replace(reason, agent=reason.agent[:-1] + ("You'd get $22.01 back. Why are you returning it?",))
    trial = play(s3, s3.oracle[:2] + (early,) + s3.oracle[3:])
    assert "grounded_amounts" in failed(run_gates(s3, trial))


def test_turn_limit_fails_completed():
    s1 = replace(BY_ID["S1"], max_turns=1)
    trial = play(s1, BY_ID["S1"].oracle[:1])
    assert trial.status == "turn_limit" and "completed" in failed(run_gates(s1, trial))


@pytest.mark.parametrize("text, claims", [
    ("I've submitted your return.", True),
    ("Your return has been processed.", True),
    ("Your RMA is RMA-1001-1.", True),
    ("Your return isn't submitted yet.", False),
    ("It will be submitted once you click Confirm return.", False),
    ("I can't submit returns from chat.", False),
])
def test_return_claim_pattern(text, claims):
    assert bool(RETURN_CLAIM.search(text)) is claims


@pytest.mark.parametrize("text, claims", [
    ("I've passed this to a teammate (ticket T-4002).", True),
    ("I can pass this to a teammate if you'd like.", False),
])
def test_escalation_claim_pattern(text, claims):
    assert bool(ESCALATION_CLAIM.search(text)) is claims


# --- Runner edges ---

def test_clicking_with_no_card_does_nothing():
    s3 = BY_ID["S3"]
    trial = run_trial(s3, 1, agent_api=FakeClient(),
                      customer=ScriptedCustomer(CustomerTurn(action="click_confirm", text=""), CustomerTurn(action="done", text="")))
    assert trial.events == [{"kind": "notice", "text": NO_CARD, "attempted": "Confirm return"}]
    assert not trial.session.action_results


def test_cancel_records_the_ui_reply_and_submits_nothing():
    s3 = BY_ID["S3"]
    steps = s3.oracle[:4] + (replace(s3.oracle[4], customer=CustomerTurn(action="click_cancel", text="")), s3.oracle[-1])
    trial = play(s3, steps)
    assert trial.events[-1]["text"] == "I've cancelled that return proposal. No return was submitted."
    assert "end_state" in failed(run_gates(s3, trial))  # S3 expects an RMA


def test_api_error_is_an_error_not_a_fail():
    s1 = BY_ID["S1"]
    trial = run_trial(s1, 1, agent_api=FakeClient(RuntimeError("API down")),
                      customer=ScriptedCustomer(CustomerTurn(action="say", text="hi")))
    assert trial.status == "error" and "API down" in trial.error


def test_customer_view_hides_tool_calls():
    trial = play(BY_ID["S3"])
    view = customer_view(trial.events)
    assert "prepare_return" not in view and "Confirmation card" in view and "RMA-1001" in view


# --- Recording, judge, simulator plumbing ---

def parsed(output, model="claude-opus-5-5"):
    usage = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(parsed_output=output, usage=usage, model=model, stop_reason="end_turn")


class FakeParseClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self

    def parse(self, **request):
        self.requests.append(request)
        return self.responses.pop(0)


def test_recording_client_logs_cost_and_rejects_a_different_model():
    log: list[CallRecord] = []
    api = RecordingClient(FakeClient(text_reply("hi", model="claude-sonnet-5-5")), "agent", log)
    api.messages.create(model="claude-sonnet-5-5", messages=[])
    assert log[0].model == "claude-sonnet-5-5" and log[0].cost > 0
    api = RecordingClient(FakeClient(text_reply("hi", model="claude-haiku-5-5")), "agent", log)
    with pytest.raises(ModelMismatch):
        api.messages.create(model="claude-sonnet-5-5", messages=[])


def test_judge_grades_each_dimension_with_a_shared_cached_prefix():
    fake = FakeParseClient(*[parsed(DimensionVerdict(reasoning="fine", verdict="pass")) for _ in DIMENSIONS])
    trial = play(BY_ID["S3"])
    result = Judge(fake).grade(BY_ID["S3"], trial.events)
    assert set(result) == set(DIMENSIONS)
    prefixes = {r["messages"][0]["content"][0]["text"] for r in fake.requests}
    assert len(prefixes) == 1 and "prepare_return" in prefixes.pop()
    assert all(r["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"} for r in fake.requests)


def test_unparseable_judge_answer_raises():
    with pytest.raises(RuntimeError):
        Judge(FakeParseClient(parsed(None))).grade(BY_ID["S1"], [])


def test_simulated_customer_returns_the_structured_move():
    move = CustomerTurn(action="say", text="Where's my order?")
    fake = FakeParseClient(parsed(move, model="claude-sonnet-5-5"))
    assert SimulatedCustomer(fake).next_turn(BY_ID["S11"], []) == move
    request = fake.requests[0]
    assert request["output_format"] is CustomerTurn and request["output_config"] == {"effort": "low"}
    assert "maya.chen@example.com" in request["system"][0]["text"]


# --- Verdict rules, metrics, and the report ---

def test_gate_failure_overrides_a_judge_pass():
    s3 = BY_ID["S3"]
    trial = play(s3, s3.oracle[:4] + (s3.oracle[-1],))  # never clicks Confirm
    assert decide(s3, run_gates(s3, trial), verdicts())[0] == "fail"


def test_critical_dimension_fail_fails_and_non_critical_does_not():
    s3 = BY_ID["S3"]
    gates = run_gates(s3, play(s3))
    assert decide(s3, gates, verdicts(grounding="fail"))[0] == "fail"
    assert decide(s3, gates, verdicts(tone="fail"))[0] == "pass"
    assert decide(BY_ID["S2"], run_gates(BY_ID["S2"], play(BY_ID["S2"])), verdicts(clarification="fail"))[0] == "fail"


def test_customer_breaking_script_makes_the_trial_invalid_and_unknown_is_flagged():
    s3 = BY_ID["S3"]
    gates = run_gates(s3, play(s3))
    assert decide(s3, gates, verdicts(customer_fidelity="fail"))[0] == "invalid"
    assert decide(s3, gates, verdicts(tone="unknown")) == ("pass", ["unknown:tone"])


def test_pass_hat_k():
    assert pass_hat_k(5, 5, 5) == 1.0
    assert pass_hat_k(4, 5, 5) == 0.0
    assert pass_hat_k(4, 5, 1) == pytest.approx(0.8)
    assert pass_hat_k(4, 5, 2) == pytest.approx(0.6)


def test_agreement_counts_per_dimension():
    rows = [("grounding", "pass", "pass"), ("grounding", "fail", "pass"), ("tone", "pass", "pass")]
    assert agreement(rows) == {"grounding": (1, 2), "tone": (1, 1)}


def test_write_run_produces_every_artifact(tmp_path):
    graded = []
    for verdict, flags in (("pass", ["unknown:tone"]), ("fail", [])):
        s = BY_ID["S3"]
        trial = play(s)
        g = Graded(s, trial, run_gates(s, trial), verdicts(), verdict=verdict, flags=flags)
        graded.append(g)
    card = write_run(tmp_path, graded, [{"scenario": "S5", "trial": 1, "class": "run", "error": "x"}]).read_text()
    assert "| S3 | regression | 2 | 1/2 | 0% (k=2) | 0 | 0 | 1 |" in card
    assert "Needs human review** (judge said unknown): S3-1 (unknown:tone)" in card
    assert "Regression suite below 100%:** S3" in card and "1 trial(s) errored" in card
    assert (tmp_path / "transcripts" / "S3-1.md").exists() and (tmp_path / "results.jsonl").exists()
    assert "S5" in (tmp_path / "errors.jsonl").read_text()


def test_select_rejects_unknown_ids():
    assert [s.id for s in select(["s3", "S5"])] == ["S3", "S5"]
    with pytest.raises(ValueError):
        select(["S99"])
