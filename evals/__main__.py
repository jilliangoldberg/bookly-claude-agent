"""CLI for the simulated-customer evals. These call the Anthropic API and cost money; they are not
part of pytest.

  python -m evals list
  python -m evals run [--scenario S3 S5] [--trials 5] [--no-judge]
  python -m evals calibrate [--labels evals/labels.jsonl]"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from evals import AGENT_MODEL, CUSTOMER_MODEL, JUDGE_MODEL
from evals.gates import run_gates
from evals.judge import Judge
from evals.recording import RecordingClient
from evals.report import Graded, decide, write_run
from evals.runner import run_trial
from evals.scenarios import SCENARIOS, select
from evals.simulator import SimulatedCustomer

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"


def cmd_list(_args) -> int:
    for s in SCENARIOS:
        print(f"{s.id:<4} {s.suite:<11} {s.title}")
    return 0


def cmd_run(args) -> int:
    scenarios = select(args.scenario)
    client = anthropic.Anthropic()
    out_dir = RESULTS / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    total = len(scenarios) * args.trials
    print(f"Running {total} trial(s): agent {AGENT_MODEL}, customer {CUSTOMER_MODEL}, "
          f"judge {'off' if args.no_judge else JUDGE_MODEL}", file=sys.stderr)

    graded, errors, done = [], [], 0
    for scenario in scenarios:
        for n in range(1, args.trials + 1):
            done += 1
            print(f"[{done}/{total}] {scenario.id}-{n} ...", end="", flush=True, file=sys.stderr)
            calls = []
            trial = run_trial(
                scenario, n,
                agent_api=RecordingClient(client, "agent", calls),
                customer=SimulatedCustomer(RecordingClient(client, "customer", calls)),
                calls=calls,
            )
            if trial.status == "error":
                errors.append({"scenario": scenario.id, "trial": n, "class": "run", "error": trial.error})
                print(f" error: {trial.error}", file=sys.stderr)
                continue
            gates = run_gates(scenario, trial)
            verdicts = None
            if not args.no_judge:
                try:
                    verdicts = Judge(RecordingClient(client, "judge", calls)).grade(scenario, trial.events)
                except (anthropic.APIError, RuntimeError) as exc:
                    errors.append({"scenario": scenario.id, "trial": n, "class": "judge",
                                   "error": f"{type(exc).__name__}: {exc}"})
                    print(f" judge error: {exc}", file=sys.stderr)
                    continue
            g = Graded(scenario, trial, gates, verdicts)
            g.verdict, g.flags = decide(scenario, gates, verdicts)
            graded.append(g)
            print(f" {g.verdict}", file=sys.stderr)

    path = write_run(out_dir, graded, errors)
    print(path.read_text())
    print(f"Saved to {out_dir.relative_to(ROOT.parent)}/", file=sys.stderr)
    return 0 if graded and all(g.verdict != "fail" for g in graded) and not errors else 1


def agreement(rows: list[tuple[str, str, str]]) -> dict[str, tuple[int, int]]:
    """Per dimension: (judge agreed with the human, labels compared). Rows are (dimension, human, judge)."""
    out: dict[str, tuple[int, int]] = {}
    for dimension, human, judged in rows:
        agreed, seen = out.get(dimension, (0, 0))
        out[dimension] = (agreed + (human == judged), seen + 1)
    return out


def cmd_calibrate(args) -> int:
    labels_path = Path(args.labels)
    labels = [json.loads(line) for line in labels_path.read_text().splitlines() if line.strip()] if labels_path.exists() else []
    if not labels:
        print(f"No labels in {labels_path}. Add lines like:\n"
              '{"transcript": "evals/results/<run>/transcripts/S3-1.json", "dimension": "grounding", '
              '"label": "pass", "note": "why"}', file=sys.stderr)
        return 1
    by_id = {s.id: s for s in SCENARIOS}
    judge = Judge(anthropic.Anthropic())
    rows, disagreements = [], []
    for label in labels:
        record = json.loads((ROOT.parent / label["transcript"]).read_text())
        scenario = by_id[record["scenario"]]
        verdict = judge.grade_one(record["judge_context"], scenario, label["dimension"])
        rows.append((label["dimension"], label["label"], verdict.verdict))
        if verdict.verdict != label["label"]:
            disagreements.append((label, verdict))
    for dimension, (agreed, seen) in sorted(agreement(rows).items()):
        print(f"{dimension:<18} {agreed}/{seen} agree ({agreed / seen:.0%})")
    for label, verdict in disagreements:
        print(f"\n- {label['transcript']} [{label['dimension']}] human={label['label']} judge={verdict.verdict}"
              f"\n  human note: {label.get('note', '')}\n  judge: {verdict.reasoning}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List scenarios.").set_defaults(func=cmd_list)
    run = sub.add_parser("run", help="Run scenarios against the live agent (spends API credits).")
    run.add_argument("--scenario", nargs="*", help="Scenario ids (default: all).")
    run.add_argument("--trials", type=int, default=5, help="Trials per scenario (default 5; pass^k needs repeats).")
    run.add_argument("--no-judge", action="store_true", help="Deterministic gates only: faster and cheaper.")
    run.set_defaults(func=cmd_run)
    cal = sub.add_parser("calibrate", help="Compare the judge with human labels (spends API credits).")
    cal.add_argument("--labels", default=str(ROOT / "labels.jsonl"))
    cal.set_defaults(func=cmd_calibrate)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
