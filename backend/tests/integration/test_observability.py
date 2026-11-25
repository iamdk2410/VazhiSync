"""B18 acceptance: you can open any past run and follow every step it took, and the report
generates in one command."""

import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from arq import ArqRedis
from arq.worker import Worker
from httpx import AsyncClient
from sqlalchemy import func, select

from app.db.models import AgentStep, ToolCallRecord
from app.db.session import get_sessionmaker
from app.runner.world import WorldRunner
from tests.conftest import Account
from tests.integration.conftest import bearer, login
from tests.integration.test_approval import (  # noqa: F401
    api,
    paused_run,
    pending,
    run_id_of,
    wait_for,
    waiting,
    worker,
)
from tests.integration.test_workflow import bunched, clock, redis  # noqa: F401

pytestmark = pytest.mark.services
BACKEND = Path(__file__).resolve().parents[2]


async def test_any_past_run_can_be_followed_step_by_step(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    worker: Worker,  # noqa: F811
    api: AsyncClient,  # noqa: F811
    accounts: dict[str, Account],
) -> None:
    result = await paused_run(job_queue, checkpointer)
    run_id = run_id_of(result)
    officer = bearer(await login(api, accounts["officer"]))
    rec = await pending(api, officer)
    await api.post(
        f"/officer/recommendations/{rec['id']}/decision",
        json={"decision": "approve"},
        headers=officer,
    )
    await wait_for(run_id, waiting("driver"))

    resp = await api.get(f"/officer/runs/{run_id}/trace", headers=officer)
    assert resp.status_code == 200, resp.text
    trace = resp.json()
    async with get_sessionmaker()() as session:
        stored_steps = await session.scalar(
            select(func.count()).select_from(AgentStep).where(AgentStep.run_id == run_id)
        )
        stored_tools = await session.scalar(
            select(func.count()).select_from(ToolCallRecord).where(ToolCallRecord.run_id == run_id)
        )

    # Every step, in order, nothing missing - across both segments (before and after approval).
    steps = trace["steps"]
    assert [s["index"] for s in steps] == list(range(1, (stored_steps or 0) + 1))
    nodes = [s["node"] for s in steps]
    assert nodes[:3] == ["headway_monitor", "action_generator", "impact_simulator"]
    assert {"control_advisor", "await_officer", "dispatch", "await_driver"} <= set(nodes)

    # Each tool call is attached to the step that made it.
    by_node = {s["node"]: s for s in steps}
    monitor_tools = [t["tool"] for t in by_node["headway_monitor"]["tool_calls"]]
    assert monitor_tools == ["get_vehicle_positions", "get_headways"]
    dispatch_tools = [t["tool"] for t in by_node["dispatch"]["tool_calls"]]
    assert "send_driver_instruction" in dispatch_tools
    linked = sum(len(s["tool_calls"]) for s in steps) + len(trace["unlinked_tool_calls"])
    assert linked == stored_tools == trace["totals"]["tool_calls"] > 0
    assert all(t["duration_ms"] >= 0 and t["attempts"] >= 1 for s in steps for t in s["tool_calls"])

    # What the humans did and what was sent.
    assert [d["decision"] for d in trace["decisions"]] == ["approved"]
    (instruction,) = trace["instructions"]
    assert instruction["action_type"] == rec["action_type"]
    assert trace["pair"]["follower"] == "T1-02" and trace["event_kind"] == "actual"
    assert trace["waiting_for"] == "driver"

    missing = await api.get(f"/officer/runs/{uuid.uuid4()}/trace", headers=officer)
    assert missing.status_code == 404
    driver = bearer(await login(api, accounts["driver"]))
    assert (await api.get(f"/officer/runs/{run_id}/trace", headers=driver)).status_code == 403

    # ---- metrics over the same data
    m = (await api.get("/officer/metrics?hours=1", headers=officer)).json()
    assert m["runs"]["total"] >= 1
    tools = {t["tool"]: t for t in m["tools"]}
    assert tools["get_headways"]["calls"] >= 2 and tools["get_headways"]["latency_ms"]["p50"] >= 0
    assert tools["send_driver_instruction"]["calls"] == 1
    assert m["runs"]["execution_s"]["p50"] is not None


def run_report(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - our own module, fixed arguments
        [sys.executable, "-m", "app.reports.generate_report", *args],
        cwd=BACKEND, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip


async def test_the_report_generates_in_one_command(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    tmp_path: Path,
) -> None:
    await paused_run(job_queue, checkpointer)  # some data to report on

    md = tmp_path / "report.md"
    done = run_report("--hours", "1", "--out", str(md))
    assert done.returncode == 0, done.stderr
    text = md.read_text("utf-8")
    for section in ("# VazhiSync - token, cost and failure report", "## Workflow runs",
                    "## LLM tokens and cost", "## Tools", "## Recent failed runs",
                    "## Failure matrix"):  # fmt: skip
        assert section in text
    assert "get_headways" in text  # the tool table has data

    pdf = tmp_path / "report.pdf"
    done = run_report("--hours", "1", "--out", str(pdf))
    assert done.returncode == 0, done.stderr
    assert pdf.read_bytes().startswith(b"%PDF-1.4")

    to_stdout = run_report("--hours", "1")
    assert to_stdout.returncode == 0 and to_stdout.stdout.startswith("# VazhiSync")
