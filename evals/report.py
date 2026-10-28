"""Verdict rules, pass rates, and the files a run leaves behind: results.jsonl, errors.jsonl,
transcripts/, and scorecard.md."""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from math import comb
from pathlib import Path

from evals.gates import GateResult
from evals.judge import DimensionVerdict, judge_context
from evals.runner import Trial
from evals.scenarios import Scenario


@dataclass
class Graded:
    """One trial plus its grades and final verdict."""

    scenario: Scenario
    trial: Trial
    gates: list[GateResult]
    verdicts: dict[str, DimensionVerdict] | None  # None when run with --no-judge
    verdict: str = ""  # pass | fail | invalid
    flags: list[str] = field(default_factory=list)  # e.g. dimensions the judge marked "unknown"


def decide(scenario: Scenario, gates: list[GateResult], verdicts: dict[str, DimensionVerdict] | None) -> tuple[str, list[str]]:
    """Gates override the judge; a customer who broke script makes the trial invalid, not failed."""
    flags = [f"unknown:{d}" for d, v in (verdicts or {}).items() if v.verdict == "unknown"]
    if verdicts and verdicts.get("customer_fidelity") and verdicts["customer_fidelity"].verdict == "fail":
        return "invalid", flags
    if not all(g.ok for g in gates):
        return "fail", flags
    if verdicts and any(verdicts[d].verdict == "fail" for d in scenario.critical_dimensions if d in verdicts):
        return "fail", flags
    return "pass", flags


def pass_hat_k(passes: int, trials: int, k: int) -> float:
    """τ-bench's pass^k: the chance that k trials drawn from these all pass."""
    return comb(passes, k) / comb(trials, k) if trials >= k else float("nan")


def trial_record(g: Graded) -> dict:
    """The full JSON for one trial (also what `calibrate` re-judges from)."""
    t = g.trial
    s = t.session
    return {
        "scenario": g.scenario.id,
        "trial": t.n,
        "status": t.status,
        "verdict": g.verdict,
        "flags": g.flags,
        "gates": [vars(x) for x in g.gates],
        "judge": {d: v.model_dump() for d, v in (g.verdicts or {}).items()},
        "end_state": {
            "verified_customer": s.verified_customer_id,
            "returns": [r for r in s.action_results.values() if r.get("status") == "submitted"],
            "ticket": s.escalation,
            "pending_action": s.pending_action.token if s.pending_action else None,
        },
        "events": t.events,
        "calls": [c.to_dict() for c in t.calls],
        "cost": round(sum(c.cost for c in t.calls), 6),
        "wall_ms": t.wall_ms,
        "judge_context": judge_context(g.scenario, t.events),
    }


def transcript_markdown(g: Graded) -> str:
    """A readable transcript: what the customer saw, tool calls indented, then the grades."""
    lines = [f"# {g.scenario.id}-{g.trial.n}: {g.scenario.title}", "",
             f"**Verdict:** {g.verdict.upper()}  ·  status `{g.trial.status}`  ·  "
             f"${sum(c.cost for c in g.trial.calls):.3f}  ·  {g.trial.wall_ms / 1000:.0f}s", ""]
    if g.flags:
        lines += [f"**Flags:** {', '.join(g.flags)}", ""]
    lines += ["## Conversation", ""]
    for e in g.trial.events:
        k = e["kind"]
        if k == "customer":
            lines.append(f"**Customer:** {e['text']}\n")
        elif k == "dewey":
            lines.append(f"**Dewey:** {e['text']}\n")
        elif k == "tool":
            status = "ok" if e["ok"] else e["output"].get("error_code", "error")
            lines.append(f"> `{e['name']}({json.dumps(e['input'])})` → {status}\n")
        elif k == "card":
            lines.append("> **Card:** " + e["text"].replace("\n", " · ") + "\n")
        elif k == "click":
            lines.append(f"**Customer pressed {e['button']}**\n")
        elif k == "action_reply":
            lines.append(f"**App:** {e['text']}\n")
        elif k == "notice":
            lines.append(f"> {e['text']}\n")
        elif k == "validation":
            lines.append(f"> 🛡️ **Reply check** ({e['outcome']}): {'; '.join(e['violations'])}. "
                         f"Rejected draft: _{e['draft']}_\n")
    lines += ["## Gates", ""]
    lines += [f"- {'✅' if x.ok else '❌'} `{x.name}` {x.detail}" for x in g.gates]
    if g.verdicts:
        lines += ["", "## Judge", ""]
        for d, v in g.verdicts.items():
            mark = {"pass": "✅", "fail": "❌", "unknown": "❔"}[v.verdict]
            crit = " (critical)" if d in g.scenario.critical_dimensions else ""
            lines.append(f"- {mark} **{d}**{crit}: {v.reasoning}")
    return "\n".join(lines) + "\n"


def scorecard(graded: list[Graded], errors: list[dict]) -> str:
    """Per-scenario pass@1 and pass^k, failing gates and dimensions, and cost by role."""
    by_scenario: dict[str, list[Graded]] = defaultdict(list)
    for g in graded:
        by_scenario[g.scenario.id].append(g)
    errored = defaultdict(int)
    for e in errors:
        errored[e["scenario"]] += 1

    lines = ["# Eval scorecard", "",
             "| Scenario | Suite | Valid | pass@1 | pass^k | Invalid | Errors | Needs review | Repaired / fallback | Failing gates / dimensions |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    regression_misses = []
    for sid, gs in by_scenario.items():
        valid = [g for g in gs if g.verdict != "invalid"]
        passes = sum(g.verdict == "pass" for g in valid)
        n = len(valid)
        reasons: dict[str, int] = defaultdict(int)
        for g in valid:
            if g.verdict == "fail":
                for x in g.gates:
                    if not x.ok:
                        reasons[x.name] += 1
                for d, v in (g.verdicts or {}).items():
                    if v.verdict == "fail" and d in g.scenario.critical_dimensions:
                        reasons[f"judge:{d}"] += 1
        p1 = f"{passes}/{n}" if n else "-"
        pk = f"{pass_hat_k(passes, n, n):.0%} (k={n})" if n else "-"
        why = ", ".join(f"{r}×{c}" for r, c in sorted(reasons.items())) or "-"
        suite = gs[0].scenario.suite
        review = sum(bool(g.flags) for g in gs)  # trials where the judge answered "unknown" somewhere
        checks = [e["outcome"] for g in gs for e in g.trial.events if e["kind"] == "validation"]
        fixes = f"{checks.count('repaired')} / {checks.count('fallback')}"
        lines.append(f"| {sid} | {suite} | {n} | {p1} | {pk} | {len(gs) - n} | {errored.get(sid, 0)} | {review} | {fixes} | {why} |")
        if suite == "regression" and n and passes < n:
            regression_misses.append(sid)

    calls = [c for g in graded for c in g.trial.calls]
    by_role: dict[str, list] = defaultdict(lambda: [0, 0, 0.0])
    for c in calls:
        row = by_role[c.role]
        row[0] += c.input_tokens + c.cache_read_tokens + c.cache_write_tokens
        row[1] += c.output_tokens
        row[2] += c.cost
    total = sum(r[2] for r in by_role.values())
    lines += ["", "## Cost (scored trials)", "", "| Role | Input tokens | Output tokens | USD |", "|---|---|---|---|"]
    lines += [f"| {role} | {r[0]:,} | {r[1]:,} | ${r[2]:.2f} |" for role, r in sorted(by_role.items())]
    per_trial = total / len(graded) if graded else 0
    lines += [f"| **total** | | | **${total:.2f}** (${per_trial:.3f}/trial) |", ""]
    if regression_misses:
        lines.append(f"**Regression suite below 100%:** {', '.join(regression_misses)}")
    flagged = [f"{g.scenario.id}-{g.trial.n} ({', '.join(g.flags)})" for g in graded if g.flags]
    if flagged:
        lines.append(f"**Needs human review** (judge said unknown): {'; '.join(flagged)}")
    if errors:
        lines.append(f"**{len(errors)} trial(s) errored and were not scored** (see errors.jsonl).")
    return "\n".join(lines) + "\n"


def write_run(out_dir: Path, graded: list[Graded], errors: list[dict]) -> Path:
    """Write every artifact for a run and return the scorecard path."""
    transcripts = out_dir / "transcripts"
    transcripts.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "results.jsonl", "w") as results:
        for g in graded:
            record = trial_record(g)
            name = f"{g.scenario.id}-{g.trial.n}"
            (transcripts / f"{name}.json").write_text(json.dumps(record, indent=2, default=str))
            (transcripts / f"{name}.md").write_text(transcript_markdown(g))
            summary = {k: record[k] for k in ("scenario", "trial", "status", "verdict", "flags", "gates", "cost", "wall_ms")}
            summary["judge"] = {d: v["verdict"] for d, v in record["judge"].items()}
            results.write(json.dumps(summary, default=str) + "\n")
    with open(out_dir / "errors.jsonl", "w") as f:
        for e in errors:
            f.write(json.dumps(e, default=str) + "\n")
    path = out_dir / "scorecard.md"
    path.write_text(scorecard(graded, errors))
    return path
