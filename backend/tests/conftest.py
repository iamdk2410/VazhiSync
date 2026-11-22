import os
from collections.abc import Iterator

import pytest

# Unit tests must not depend on a developer's .env: provide the required settings up front.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5434/test")
os.environ.setdefault("JWT_SECRET", "unit-test-secret-at-least-16-chars")


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
