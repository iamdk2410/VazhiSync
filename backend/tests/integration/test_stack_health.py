"""B1 acceptance: `docker compose up` starts all services and they pass health checks.

Run after `docker compose up -d` (from the repo root):  uv run pytest -m stack
"""

import json
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.stack

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_SERVICES = {"postgres", "redis", "api", "worker", "avl-mcp", "demand-mcp", "sim-runner"}
WAIT_SECONDS = 120


def _compose_ps() -> list[dict[str, str]]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker not installed")
    out = subprocess.run(  # noqa: S603  (fixed argv, no shell)
        [docker, "compose", "ps", "--all", "--format", "json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()
    if not out:
        return []
    # Compose prints either one JSON array or one JSON object per line.
    if out.startswith("["):
        return list(json.loads(out))
    return [json.loads(line) for line in out.splitlines()]


def _health_by_service() -> dict[str, str]:
    return {c["Service"]: c.get("Health") or c.get("State", "") for c in _compose_ps()}


def test_all_core_services_healthy() -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    health = _health_by_service()
    if not health:
        pytest.skip("stack not running: docker compose up -d")
    while time.monotonic() < deadline:
        health = _health_by_service()
        if all(health.get(s) == "healthy" for s in CORE_SERVICES):
            break
        time.sleep(3)
    assert {s: health.get(s) for s in CORE_SERVICES} == dict.fromkeys(CORE_SERVICES, "healthy")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/health",
        "http://127.0.0.1:8101/health",
        "http://127.0.0.1:8102/health",
    ],
)
def test_health_endpoints_reachable_from_host(url: str) -> None:
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310  (fixed http URL)
        assert resp.status == 200
        assert json.loads(resp.read())["status"] == "ok"
