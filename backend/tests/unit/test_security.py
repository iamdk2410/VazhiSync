import time
import uuid

import jwt
import pytest

from app.auth.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.db.enums import Role

SECRET = "unit-test-secret-at-least-16-chars"  # matches tests/conftest.py


def test_password_hash_is_argon2_and_verifies() -> None:
    hashed = hash_password("correct horse")
    assert hashed.startswith("$argon2id$")
    assert verify_password("correct horse", hashed)[0] is True
    assert verify_password("wrong horse", hashed)[0] is False


def test_token_round_trip_carries_role() -> None:
    user_id = uuid.uuid4()
    token, expires_in = create_access_token(user_id, Role.DRIVER)
    payload = decode_access_token(token)
    assert payload.sub == user_id
    assert payload.role is Role.DRIVER
    assert expires_in == 60 * 60


def _forge(claims: dict[str, object], key: str = SECRET, algorithm: str = "HS256") -> str:
    return jwt.encode(claims, key, algorithm=algorithm)


def _claims(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    base: dict[str, object] = {
        "sub": str(uuid.uuid4()),
        "role": "officer",
        "iat": now,
        "exp": now + 60,
    }
    return base | overrides


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(_forge(_claims(exp=int(time.time()) - 5)), id="expired"),
        pytest.param(
            _forge(_claims(), key="some-other-secret-value-that-is-32-bytes-plus"), id="wrong-key"
        ),
        pytest.param(_forge(_claims(role="superuser")), id="unknown-role"),
        pytest.param(_forge(_claims(sub="not-a-uuid")), id="bad-sub"),
        pytest.param(
            _forge({k: v for k, v in _claims().items() if k != "role"}), id="missing-role"
        ),
        pytest.param(
            jwt.encode(_claims(), key=None, algorithm="none"),  # type: ignore[arg-type]
            id="alg-none",
        ),
        pytest.param("garbage", id="garbage"),
    ],
)
def test_invalid_tokens_rejected(token: str) -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


def test_valid_forged_token_with_right_key_decodes() -> None:
    """Control case for the table above."""
    assert decode_access_token(_forge(_claims())).role is Role.OFFICER
