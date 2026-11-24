"""B5 acceptance with the real local LLM (Ollama + qwen3:8b on the host).

    uv run pytest -m ollama

"Monsoon evening, IPL at Chepauk" must produce a valid scenario that loads into the simulator
and changes how it behaves. Not run in CI (no GPU / model there); the same pipeline is covered
with a fake model in tests/unit/test_scenarios.py and tests/integration/test_lab.py.
"""

import httpx
import pytest

from app.config import get_settings
from app.llm.ollama import OllamaClient
from scenario_gen.checks import ScenarioContext, problems, to_sim_config
from scenario_gen.director import generate
from scenario_gen.preview import preview
from simulator import Simulation
from tests.sim_fixtures import route_32b

pytestmark = pytest.mark.ollama

CHEPAUK_STOPS = {"MTC-1091", "MTC-9547", "MTC-9511", "MTC-1090", "MTC-9553", "MTC-9512"}


def _ollama_url() -> str:
    url = get_settings().ollama_url.replace("host.docker.internal", "localhost")
    try:
        httpx.get(f"{url}/api/version", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(f"Ollama is not reachable at {url}: {exc!r}")
    return url


async def test_monsoon_evening_ipl_at_chepauk() -> None:
    ctx = ScenarioContext.for_route(route_32b())
    model = OllamaClient(_ollama_url(), get_settings().local_model, timeout_s=600)
    result = await generate("Monsoon evening, IPL at Chepauk", ctx, model, seed=2024)

    for attempt in result.attempts:
        print(
            f"attempt {attempt.number}: {attempt.reply.latency_ms} ms, problems {attempt.problems}"
        )
    assert result.scenario is not None, f"no valid scenario: {result.problems}"
    plan = result.scenario.plan
    print(plan.model_dump_json(indent=1))

    # Valid: passes every plausibility rule, and reflects the description.
    assert problems(plan, ctx) == []
    assert plan.weather, "a monsoon evening should have rain"
    assert any(set(s.stop_ids) & CHEPAUK_STOPS for s in plan.surges), "a crowd near Chepauk"
    assert 15 * 60 * 60 <= plan.start_time_s <= 22 * 60 * 60  # evening
    assert plan.breakdowns == [] and plan.gps_dropouts == []  # not in the description

    # Loads into the simulator...
    sim = Simulation.new(ctx.route, to_sim_config(plan, ctx), result.scenario.seed)
    sim.step(600)

    # ...and changes how it behaves compared with a normal day at the same time.
    p = preview(result.scenario, ctx)
    print("normal  ", p.baseline)
    print("scenario", p.scenario)
    assert p.scenario != p.baseline
    assert (p.scenario.avg_wait_s or 0) > (p.baseline.avg_wait_s or 0)
