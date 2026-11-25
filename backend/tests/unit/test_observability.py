"""B18 without services: trace linking in Langfuse, the PDF writer, percentiles."""

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from app.harness.budget import RunBudget
from app.harness.client import Availability
from app.harness.harness import ToolHarness
from app.harness.telemetry import ToolTelemetry
from app.llm.langfuse import LangfuseTracer, set_tracer
from app.observability.tracing import current_step, observation_id, step_scope
from app.officer.observability import _pct
from app.reports.pdf import text_to_pdf
from tests.unit.test_reliability import Healthy


def tracer_capturing() -> tuple[LangfuseTracer, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []

    def ingest(request: httpx.Request) -> httpx.Response:
        events.extend(json.loads(request.content)["batch"])
        return httpx.Response(207)

    return LangfuseTracer("http://lf", "pk", "sk", transport=httpx.MockTransport(ingest)), events


async def test_tool_calls_and_steps_form_one_linked_trace() -> None:
    tracer, events = tracer_capturing()
    set_tracer(tracer)
    try:
        run_id = uuid.uuid4()
        harness = ToolHarness(
            Healthy(), timeout_s=2, attempts=2, availability=Availability(30),
            telemetry=ToolTelemetry(None),
        )  # fmt: skip
        budget = RunBudget(run_id, 60, 10, 10)
        with step_scope(str(run_id), 3, "headway_monitor"):
            assert current_step() is not None
            await harness.call(
                budget, "headway_monitor", "get_vehicle_positions", {"route_id": "x"}
            )
        assert current_step() is None  # the scope ends with the step
        tracer.span(
            run_id=str(run_id), name="headway_monitor", started=datetime.now(UTC),
            observation_id=observation_id(str(run_id), 3),
        )  # fmt: skip
        await tracer.flush()
    finally:
        set_tracer(None)

    traces = [e for e in events if e["type"] == "trace-create"]
    spans = [e["body"] for e in events if e["type"] == "span-create"]
    assert len(traces) == 1 and traces[0]["body"]["id"] == str(run_id)  # one trace per run
    tool, step = spans
    assert tool["name"] == "headway_monitor:get_vehicle_positions"
    assert tool["traceId"] == step["traceId"] == str(run_id)
    assert tool["parentObservationId"] == step["id"] == observation_id(str(run_id), 3)
    assert tool["output"] == {"status": "ok", "attempts": 1}


async def test_an_unreachable_langfuse_pauses_instead_of_timing_out_every_call() -> None:
    now = [0.0]
    attempts: list[int] = []

    def down(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError("no langfuse", request=request)

    tracer = LangfuseTracer(
        "http://lf", "pk", "sk", transport=httpx.MockTransport(down), clock=lambda: now[0]
    )
    for _ in range(5):
        tracer.span(run_id=str(uuid.uuid4()), name="x", started=datetime.now(UTC))
        await tracer.flush()
    assert len(attempts) == 1  # tried once, then paused
    now[0] = 61.0
    tracer.span(run_id=str(uuid.uuid4()), name="x", started=datetime.now(UTC))
    await tracer.flush()
    assert len(attempts) == 2  # tries again after the pause


def test_the_pdf_is_a_valid_paginated_document() -> None:
    text = "# Report\n" + "\n".join(
        f"line {i} (with parentheses) and a back\\slash" for i in range(200)
    )
    pdf = text_to_pdf(text + "\nnon-latin: தமிழ்", "Test report")
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    pages = int(re.search(rb"/Count (\d+)", pdf).group(1))  # type: ignore[union-attr]
    assert pages == 3  # 200+ lines at 70 per page
    xref = int(pdf.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    assert pdf[xref : xref + 4] == b"xref"
    assert b"\\(with parentheses\\)" in pdf and b"back\\\\slash" in pdf
    assert b"non-latin: ????" in pdf  # Tamil is replaced, not a crash


def test_percentiles() -> None:
    p = _pct([10.0, 20.0, 30.0, 40.0, 100.0])
    assert (p.p50, p.max) == (30.0, 100.0) and 40.0 < (p.p95 or 0) <= 100.0
    assert _pct([]).p50 is None
