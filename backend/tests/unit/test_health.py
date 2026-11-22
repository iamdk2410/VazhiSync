import asyncio

import pytest
from fastapi.testclient import TestClient

from app.api import health
from app.main import create_app


async def _ok() -> None:
    return None


async def _fail() -> None:
    raise ConnectionError("down")


async def _hang() -> None:
    await asyncio.sleep(60)


def _client(monkeypatch: pytest.MonkeyPatch, **checks: object) -> TestClient:
    monkeypatch.setattr(health, "CHECKS", checks)
    monkeypatch.setattr(health, "CHECK_TIMEOUT_SECONDS", 0.1)
    return TestClient(create_app())


def test_health_ok_when_all_dependencies_up(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _client(monkeypatch, database=_ok, redis=_ok).get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


def test_health_503_when_a_dependency_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _client(monkeypatch, database=_fail, redis=_ok).get("/health")
    assert resp.status_code == 503
    assert resp.json()["checks"] == {"database": "error", "redis": "ok"}


def test_health_check_times_out_instead_of_hanging(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _client(monkeypatch, database=_ok, redis=_hang).get("/health")
    assert resp.status_code == 503
    assert resp.json()["checks"]["redis"] == "error"
