"""Token/cost and failure report (B18), in one command:

    uv run python -m app.reports.generate_report                 # last 24 h, Markdown to stdout
    uv run python -m app.reports.generate_report --hours 168 --out report.md
    uv run python -m app.reports.generate_report --format pdf --out report.pdf

Built from the app's own tables (llm_usage, tool_calls, agent_steps, workflow_runs), the source
of truth for reports (decision 5); Langfuse is not needed.
"""

import argparse
import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import REPO_ROOT
from app.db.models import WorkflowRun
from app.db.session import get_sessionmaker
from app.officer.observability import Metrics, Percentiles, metrics
from app.reports.pdf import text_to_pdf

MATRIX = REPO_ROOT / "docs" / "failure-matrix.md"
RECENT_FAILURES = 15


def _ms(p: Percentiles) -> str:
    return "-" if p.p50 is None else f"{p.p50:.0f} / {p.p95:.0f} / {p.max:.0f}"


def _s(p: Percentiles) -> str:
    return "-" if p.p50 is None else f"{p.p50:.1f} / {p.p95:.1f} / {p.max:.1f}"


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.1%}"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["_none in this window_", ""]
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return [*out, ""]


def matrix_summary() -> list[str]:
    if not MATRIX.exists():
        return []
    rows = [
        line
        for line in MATRIX.read_text("utf-8").splitlines()
        if line.startswith("| ") and not line.startswith("| Failure")
    ]
    tested = sum(1 for r in rows if re.search(r"::test_\w+|CI step", r.rsplit("|", 2)[-2]))
    return [
        "## Failure matrix",
        "",
        f"{len(rows)} failure cases documented in `docs/failure-matrix.md`; {tested} name an "
        "automated test (checked by `tests/unit/test_failure_matrix.py`).",
        "",
    ]


async def recent_failures(session: AsyncSession, since: datetime) -> list[list[str]]:
    runs = await session.scalars(
        select(WorkflowRun)
        .where(WorkflowRun.started_at >= since, WorkflowRun.status.in_(["failed", "timed_out"]))
        .order_by(WorkflowRun.started_at.desc())
        .limit(RECENT_FAILURES)
    )
    return [
        [
            f"`{str(r.id)[:8]}`",
            r.started_at.strftime("%Y-%m-%d %H:%M"),
            r.status.value,
            r.end_reason or "-",
            (r.error or "-")[:80].replace("|", "/"),
        ]
        for r in runs
    ]


def render(m: Metrics, failures: list[list[str]]) -> str:
    r = m.runs
    out = [
        "# VazhiSync - token, cost and failure report",
        "",
        f"Window: {m.since:%Y-%m-%d %H:%M} to {m.until:%Y-%m-%d %H:%M} UTC.",
        "",
        "## Workflow runs",
        "",
        f"- Runs: {r.total} ({r.finished} finished); success rate {_pct(r.success_rate)}, "
        f"failure rate {_pct(r.failure_rate)}",
        f"- Duration p50 / p95 / max: {_s(r.duration_s)} s (start to finish, incl. waiting "
        f"for people); agents' execution time: {_s(r.execution_s)} s",
        "",
        *_table(["Status", "Runs"], [[k, str(v)] for k, v in sorted(r.by_status.items())]),
        *_table(["End reason", "Runs"], [[k, str(v)] for k, v in r.by_end_reason.items()]),
        "## LLM tokens and cost",
        "",
        f"- Per run: {m.tokens_per_run if m.tokens_per_run is not None else '-'} tokens, "
        f"${m.cost_per_run_usd if m.cost_per_run_usd is not None else 0:.6f}",
        "",
        "### By agent",
        "",
        *_table(
            ["Agent", "Model calls", "OK", "Rules fallbacks", "Tokens in", "Tokens out", "Cost $"],
            [
                [
                    a.agent,
                    str(a.calls),
                    str(a.successes),
                    str(a.fallbacks_to_rules),
                    str(a.input_tokens),
                    str(a.output_tokens),
                    f"{a.cost_usd:.6f}",
                ]
                for a in m.llm_by_agent
            ],
        ),
        "### By model",
        "",
        *_table(
            ["Model", "Calls", "OK", "Tokens in", "Tokens out", "Cost $", "Latency ms p50/p95/max"],
            [
                [
                    x.model,
                    str(x.calls),
                    str(x.successes),
                    str(x.input_tokens),
                    str(x.output_tokens),
                    f"{x.cost_usd:.6f}",
                    _ms(x.latency_ms),
                ]
                for x in m.llm_by_model
            ],
        ),
        "## Tools",
        "",
        *_table(
            [
                "Tool",
                "Calls",
                "Failures",
                "Failure rate",
                "Retries",
                "Latency ms p50/p95/max",
                "Errors",
            ],
            [
                [
                    t.tool,
                    str(t.calls),
                    str(t.failures),
                    _pct(t.failure_rate),
                    str(t.retries),
                    _ms(t.latency_ms),
                    ", ".join(f"{k} x{v}" for k, v in t.errors.items()) or "-",
                ]
                for t in m.tools
            ],
        ),
        "## Recent failed runs",
        "",
        *_table(["Run", "Started", "Status", "End reason", "Error"], failures),
        *matrix_summary(),
    ]
    return "\n".join(out).rstrip("\n") + "\n"


async def build(hours: float) -> str:
    async with get_sessionmaker()() as session:
        m = await metrics(session, hours)
        failures = await recent_failures(session, m.since)
    return render(m, failures)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--hours", type=float, default=24.0, help="window length (default 24)")
    parser.add_argument("--format", choices=["md", "pdf"], default=None)
    parser.add_argument("--out", type=Path, default=None, help="file (default: stdout, Markdown)")
    args = parser.parse_args(argv)
    fmt = args.format or ("pdf" if args.out and args.out.suffix.lower() == ".pdf" else "md")
    if fmt == "pdf" and args.out is None:
        parser.error("--format pdf needs --out")
    if sys.platform == "win32":  # psycopg / asyncpg are happiest on the selector loop
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    text = asyncio.run(build(args.hours))
    if fmt == "pdf":
        args.out.write_bytes(text_to_pdf(text, "VazhiSync report"))
    elif args.out:
        args.out.write_text(text, "utf-8")
    else:
        sys.stdout.write(text)
    if args.out:
        print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
