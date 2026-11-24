"""ARQ Redis settings shared by every queue producer and consumer."""

import dataclasses

from arq.connections import RedisSettings

from app.config import get_settings

CONNECT_TIMEOUT_S = 5  # arq's default (1 s) is too tight on Windows, where localhost tries IPv6


def arq_redis_settings() -> RedisSettings:
    settings = RedisSettings.from_dsn(get_settings().redis_url)
    return dataclasses.replace(settings, conn_timeout=CONNECT_TIMEOUT_S)
