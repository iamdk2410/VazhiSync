"""Run the evaluation (B19) and write the results table, charts and raw numbers:

    uv run python -m app.eval.run                      # all golden scenarios, 3 seeds each
    uv run python -m app.eval.run --seeds 1 --scenarios normal_morning_peak --out /tmp/eval

Writes `docs/eval/results.md` (the table and an honest reading of it), `results.json` and
three SVG charts. Offline: no database, MCP servers or LLM (see app/eval/ab.py).
"""

import argparse
import json
import statistics
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import REPO_ROOT
from app.eval.ab import ARMS, Arm, play
from app.eval.charts import LABELS, grouped_bars
from scenario_gen.golden import load_golden
from simulator import RouteModel

ROUTE_FIXTURE = REPO_ROOT / "backend" / "tests" / "fixtures" / "route_32b.json"
DEFAULT_OUT = REPO_ROOT / "docs" / "eval"


def _route() -> RouteModel:
    return RouteModel.from_dict(json.loads(ROUTE_FIXTURE.read_text("utf-8")))


def _job(args: tuple[str, Arm, int, int]) -> dict[str, Any]:
    key, arm, seed, minutes = args
    golden = next(g for g in load_golden() if g.key == key)
    return play(golden, _route(), arm, seed, max_minutes=minutes).as_dict()


def _mean(values: list[float | None]) -> float | None:
    real = [v for v in values if v is not None]
    return round(statistics.fmean(real), 3) if real else None


def _change(a: float | None, b: float | None) -> str:
    if a is None or b is None or a == 0:
        return "-"
    return f"{(b - a) / a:+.0%}"


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        by[(r["scenario"], r["arm"])].append(r)
    scenarios = sorted({r["scenario"] for r in results})
    table: dict[str, dict[str, dict[str, float | None]]] = {}
    for s in scenarios:
        table[s] = {}
        for arm in ARMS:
            rows = by.get((s, arm), [])
            if not rows:
                continue
            table[s][arm] = {
                "avg_wait_s": _mean([r["avg_wait_s"] for r in rows]),
                "passenger_wait_minutes": _mean([r["passenger_wait_minutes"] for r in rows]),
                "headway_cv": _mean([r["headway_cv"] for r in rows]),
                "bunching_count": _mean([r["bunching_count"] for r in rows]),
                "interventions": _mean([len(r["interventions"]) for r in rows]),
                "unsafe": sum(r["unsafe"] for r in rows),
                "invalid": sum(r["invalid"] for r in rows),
            }
    acts = [i for r in results for i in r["interventions"]]
    errors = [abs(i["predicted_benefit"] - i["actual_benefit"]) for i in acts]
    return {
        "scenarios": scenarios,
        "table": table,
        "interventions": len(acts),
        "prediction": {
            "mean_abs_error": round(statistics.fmean(errors), 1) if errors else None,
            "predicted_total": round(sum(i["predicted_benefit"] for i in acts), 1),
            "actual_total": round(sum(i["actual_benefit"] for i in acts), 1),
            "helped": sum(1 for i in acts if i["actual_benefit"] > 0),
        },
    }


def render(summary: dict[str, Any], seeds: int, minutes: int) -> str:
    t = summary["table"]
    lines = [
        "# VazhiSync - evaluation results (B19)",
        "",
        f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by `uv run python -m app.eval.run`. "
        f"Each golden scenario on route 32B, up to {minutes} simulated minutes, {seeds} world "
        "seed(s) per scenario; numbers are means over the seeds. The advisor is auto-approved "
        "(evaluation mode), sees only what the MCP tools would report, and does not know the "
        "scenario's weather, surges or breakdowns. No LLM: candidate ideas and wording don't "
        "change the simulator's ranking (rule 2), so the policy is the same without it.",
        "",
        "| Scenario | Arm | Avg wait (s) | Change | Headway CV | Change | Bunching | "
        "Interventions | Unsafe | Invalid |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summary["scenarios"]:
        base = t[s].get("control", {})
        for arm, row in t[s].items():
            is_base = arm == "control"
            wait_change = "" if is_base else _change(base.get("avg_wait_s"), row["avg_wait_s"])
            cv_change = "" if is_base else _change(base.get("headway_cv"), row["headway_cv"])
            lines.append(
                f"| {s.replace('_', ' ')} | {LABELS[arm]} | {row['avg_wait_s']} | {wait_change} | "
                f"{row['headway_cv']} | {cv_change} | {row['bunching_count']} | "
                f"{row['interventions']} | {row['unsafe']} | {row['invalid']} |"
            )
    p = summary["prediction"]
    lines += [
        "",
        "## Predicted vs actual benefit",
        "",
        f"{summary['interventions']} interventions were applied. The advisor predicted "
        f"{p['predicted_total']} passenger-minutes saved in total; replaying the world with and "
        f"without each action (same random numbers) measured {p['actual_total']}. Mean absolute "
        f"error per intervention: {p['mean_abs_error']} passenger-minutes; "
        f"{p['helped']} of {summary['interventions']} actually helped.",
        "",
        "## Charts",
        "",
        "![Average wait](avg_wait.svg)",
        "",
        "![Headway variation](headway_cv.svg)",
        "",
        "![Bunching episodes](bunching.svg)",
        "",
        "## Reading the results",
        "",
        *reading(summary),
        "",
        "## Limits of this evaluation",
        "",
        "- It is the advisor's *policy* (its own proposal, rulebook, simulation and ranking code) "
        "in the loop of a simulated world, auto-approved. The whole system - world runner, MCP "
        "servers, LangGraph workflow, database, decision service, the real send guards - is "
        "exercised by `app/eval/system.py` (tests/integration/test_eval_system.py), which scores "
        "unsafe or refused sends, predicted-vs-actual error and LLM cost.",
        "- The world and the advisor's what-if model share the same simulator, so the advisor's "
        "model of bus movement is better than it would be on real streets. The advisor does not "
        "know the scenario's weather, surges or breakdowns, which is where predictions miss.",
        f"- Predictions are optimistic: in total they promised {_ratio(summary)} of what the "
        "replays measured, and about a third of interventions did not help on their own. The "
        "ranking still improved every scenario's average wait, but the officer's card should be "
        "read as an estimate.",
        "- Early warning acts more often; on the monsoon evening it made headways less regular "
        "(see above). One route, simulated data, three seeds per scenario: indicative, not "
        "statistically conclusive.",
        "",
    ]
    return "\n".join(lines)


def _ratio(summary: dict[str, Any]) -> str:
    p = summary["prediction"]
    if not p["actual_total"]:
        return "-"
    return f"{p['predicted_total'] / p['actual_total']:.1f}x"


def _rel(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None or a == 0 else (b - a) / a


def _verdict(base: dict[str, Any], arm: dict[str, Any], name: str) -> str:
    wait = _rel(base["avg_wait_s"], arm["avg_wait_s"])
    if wait is None:
        return f"{name}: no data"
    if wait <= -0.02:
        text = f"{name} helped (average wait {wait:+.0%}"
    elif wait < 0.02:
        text = f"{name} made no real difference to waiting ({wait:+.0%}"
    else:
        text = f"{name} did NOT help (average wait {wait:+.0%}"
    worse = []
    cv = _rel(base["headway_cv"], arm["headway_cv"])
    if cv is not None and cv > 0.02:
        worse.append(f"headway variation {cv:+.0%}")
    if (arm["bunching_count"] or 0) > (base["bunching_count"] or 0):
        worse.append(f"bunching {base['bunching_count']:g} -> {arm['bunching_count']:g}")
    text += "; worse: " + ", ".join(worse) if worse else ""
    if arm["invalid"]:
        text += (
            f"; {arm['invalid']} recommended action(s) couldn't be carried out by the world "
            "(e.g. for a broken-down bus the advisor can't see)"
        )
    return text + ")"


def reading(summary: dict[str, Any]) -> list[str]:
    """Plain conclusions per scenario, including where the advisor did not help."""
    out = []
    for s, arms in summary["table"].items():
        base = arms.get("control")
        if not base:
            continue
        parts = [
            _verdict(base, arms[arm], label)
            for arm, label in (("advisor", "the advisor"), ("advisor_ew", "with early warning"))
            if arm in arms
        ]
        out.append(f"- **{s.replace('_', ' ')}**: " + "; ".join(parts) + ".")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A/B evaluation over the golden scenarios")
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--scenarios", nargs="*", default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--from-json", type=Path, default=None, help="re-render a previous results.json"
    )
    args = parser.parse_args(argv)
    if args.from_json:
        saved = json.loads(args.from_json.read_text("utf-8"))
        write(saved["runs"], args.out, args.seeds, args.minutes)
        return 0
    goldens = [g for g in load_golden() if not args.scenarios or g.key in args.scenarios]
    jobs = [
        (g.key, arm, g.seed + 1000 * k, args.minutes)
        for g in goldens
        for k in range(args.seeds)
        for arm in ARMS
    ]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(_job, jobs))
    write(results, args.out, args.seeds, args.minutes)
    return 0


def write(results: list[dict[str, Any]], out: Path, seeds: int, minutes: int) -> None:
    summary = summarize(results)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(
        json.dumps({"summary": summary, "runs": results}, indent=1), "utf-8"
    )
    (out / "results.md").write_text(render(summary, seeds, minutes), "utf-8")
    t = summary["table"]
    names = summary["scenarios"]
    for file, metric, title, unit in (
        ("avg_wait.svg", "avg_wait_s", "Average passenger wait", "seconds"),
        ("headway_cv.svg", "headway_cv", "Headway variation (CV)", "coefficient of variation"),
        ("bunching.svg", "bunching_count", "Bunching episodes", "episodes per run"),
    ):
        series: dict[str, list[float | None]] = {
            arm: [t[s].get(arm, {}).get(metric) for s in names] for arm in ARMS
        }
        (out / file).write_text(grouped_bars(title, unit, names, series), "utf-8")
    print(f"wrote {out / 'results.md'}")


if __name__ == "__main__":
    raise SystemExit(main())
