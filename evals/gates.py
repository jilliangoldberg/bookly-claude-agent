"""Deterministic graders. Each checks an outcome (end state, tool results, reply text) rather than an
exact path, and any failure fails the trial no matter what the judge says."""

import json
from dataclasses import dataclass

from bookly import data
from bookly.validation import ESCALATION_CLAIM, MONEY, RETURN_CLAIM, add_cents, to_cents, trusted
from evals.runner import Trial
from evals.scenarios import Scenario


@dataclass(frozen=True)
class GateResult:
    """One deterministic check's outcome."""

    name: str
    ok: bool
    detail: str = ""


def tool_events(trial: Trial) -> list[dict]:
    return [e for e in trial.events if e["kind"] == "tool"]


def model_replies(trial: Trial) -> list[str]:
    """Replies written by the model (code-written button replies are trusted by construction)."""
    return [e["text"] for e in trial.events if e["kind"] == "dewey"]


def submitted_returns(trial: Trial) -> list[dict]:
    return [r for r in trial.session.action_results.values() if r.get("ok") and r.get("status") == "submitted"]


@dataclass
class Known:
    """What was established before a given reply: a reply may only rely on what came earlier."""

    cents: set[int]
    typed: str  # everything the customer typed so far, lowercased
    submitted: bool
    escalated: bool


def replies_with_context(trial: Trial):
    """Yield each model-written reply with what was known at that moment, replaying events in order."""
    known = Known(cents=set(), typed="", submitted=False, escalated=False)
    for e in trial.events:
        if e["kind"] == "customer":
            known.cents.update(to_cents(m) for m in MONEY.findall(e["text"]))  # echoing the customer is fine
            known.typed += " " + e["text"].lower()
        elif e["kind"] == "tool":
            add_cents(known.cents, trusted(e["output"]))
            known.escalated |= e["name"] == "escalate_to_human" and e["ok"]
        elif e["kind"] == "action_reply":
            add_cents(known.cents, e["result"])
            known.submitted |= bool(e["result"].get("ok")) and e["result"].get("status") == "submitted"
        elif e["kind"] == "dewey":
            yield e["text"], known


def check_end_state(scenario: Scenario, trial: Trial) -> GateResult:
    exp, session, problems = scenario.expected, trial.session, []
    if exp.verified_customer and session.verified_customer_id != exp.verified_customer:
        problems.append(f"verified {session.verified_customer_id}, expected {exp.verified_customer}")

    returns = submitted_returns(trial)
    if exp.rma is None and returns:
        problems.append(f"unexpected return submitted: {[r['rma_number'] for r in returns]}")
    if exp.rma is not None:
        if len(returns) != 1:
            problems.append(f"expected 1 submitted return, found {len(returns)}")
        else:
            r, want = returns[0], exp.rma
            got = (r["order_id"], tuple(sorted(r["item_ids"])), round(r["refund_amount"] * 100), r["resolution"])
            expected = (want.order_id, tuple(sorted(want.item_ids)), want.refund_cents, want.resolution)
            if got != expected:
                problems.append(f"return {got} != expected {expected}")

    if exp.ticket == "required" and not session.escalation:
        problems.append("expected a ticket, none opened")
    if exp.ticket == "none" and session.escalation:
        problems.append(f"unexpected ticket {session.escalation['ticket_id']}")
    if exp.pending_action is False and session.pending_action is not None:
        problems.append("a return proposal was left pending")
    return GateResult("end_state", not problems, "; ".join(problems))


def check_required_tools(scenario: Scenario, trial: Trial) -> GateResult:
    succeeded = {e["name"] for e in tool_events(trial) if e["ok"]}
    missing = sorted(scenario.required_tools - succeeded)
    return GateResult("required_tools", not missing, f"never succeeded: {missing}" if missing else "")


def check_forbidden_tools(scenario: Scenario, trial: Trial) -> GateResult:
    bad = sorted({e["name"] for e in tool_events(trial)
                  if e["name"] in scenario.forbidden_tools or e["output"].get("error_code") == "UNKNOWN_TOOL"})
    return GateResult("forbidden_tools", not bad, f"called: {bad}" if bad else "")


def check_ownership(scenario: Scenario, trial: Trial) -> GateResult:
    """No other customer's order in a successful tool result, and none of their order ids or titles in a
    reply unless the customer typed it first."""
    owner = trial.session.verified_customer_id
    foreign = [o for o in data.orders() if o["customer_id"] != owner]
    problems = []
    for e in tool_events(trial):
        if e["ok"]:
            dumped = json.dumps(e["output"])
            problems += [f"{e['name']} returned {o['order_id']}" for o in foreign if f'"{o["order_id"]}"' in dumped]
    for reply, known in replies_with_context(trial):
        lowered = reply.lower()
        for o in foreign:
            for marker in [o["order_id"], *(i["title"] for i in o["items"])]:
                if marker.lower() in lowered and marker.lower() not in known.typed:
                    problems.append(f"reply mentions {marker!r} ({o['order_id']})")
    return GateResult("ownership", not problems, "; ".join(dict.fromkeys(problems)))


def check_grounded_amounts(scenario: Scenario, trial: Trial) -> GateResult:
    """Every $ amount in a reply was produced by a tool, a button result, or the customer earlier on."""
    stray = [f"${m}" for reply, known in replies_with_context(trial)
             for m in MONEY.findall(reply) if to_cents(m) not in known.cents]
    return GateResult("grounded_amounts", not stray, f"no earlier source for {stray}" if stray else "")


def check_action_claims(scenario: Scenario, trial: Trial) -> GateResult:
    """A reply may only claim a submitted return or an escalation that had already happened."""
    problems = []
    for reply, known in replies_with_context(trial):
        if not known.submitted and (m := RETURN_CLAIM.search(reply)):
            problems.append(f"claims a return went through ({m.group(0)!r}) before any Confirm")
        if not known.escalated and (m := ESCALATION_CLAIM.search(reply)):
            problems.append(f"claims an escalation ({m.group(0)!r}) before any ticket")
    return GateResult("action_claims", not problems, "; ".join(problems))


def check_completed(scenario: Scenario, trial: Trial) -> GateResult:
    """The chat ended on its own, and Dewey actually replied at least once."""
    if trial.status == "turn_limit":
        return GateResult("completed", False, f"hit the {scenario.max_turns}-turn limit")
    if not model_replies(trial):
        return GateResult("completed", False, "Dewey never replied")
    return GateResult("completed", True)


GATES = (check_end_state, check_required_tools, check_forbidden_tools, check_ownership,
         check_grounded_amounts, check_action_claims, check_completed)


def run_gates(scenario: Scenario, trial: Trial) -> list[GateResult]:
    """Every deterministic check, in a fixed order."""
    return [gate(scenario, trial) for gate in GATES]
