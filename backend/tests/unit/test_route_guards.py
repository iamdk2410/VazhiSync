"""Rule 10: every endpoint is guarded on the server unless explicitly public.

Behavioural check over the OpenAPI schema (a stable interface, unlike router internals):
each non-public operation called without a token must answer 401 before touching data.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.api.schemas import RegisterRequest
from app.main import create_app

PUBLIC = {
    ("GET", "/health"),
    ("POST", "/auth/register"),
    ("POST", "/auth/login"),
}

_app = create_app()
_OPERATIONS = sorted(
    (method.upper(), path)
    for path, item in _app.openapi()["paths"].items()
    for method in item
    if method in {"get", "post", "put", "patch", "delete"}
)


def test_schema_lists_the_expected_endpoints() -> None:
    """Guards against the check below passing vacuously."""
    assert len(_OPERATIONS) >= 8
    assert set(_OPERATIONS) >= PUBLIC


@pytest.mark.parametrize(
    ("method", "path"), [op for op in _OPERATIONS if op not in PUBLIC], ids=" ".join
)
def test_endpoint_requires_authentication(method: str, path: str) -> None:
    concrete = re.sub(r"\{[^}]+\}", "STOP_1", path)
    resp = TestClient(_app).request(method, concrete, json={})
    assert resp.status_code == 401, f"{method} {path} answered {resp.status_code} without a token"


def test_register_schema_has_no_role_field() -> None:
    assert "role" not in RegisterRequest.model_fields
    assert RegisterRequest.model_config.get("extra") == "forbid"
