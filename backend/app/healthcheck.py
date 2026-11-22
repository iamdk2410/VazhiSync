"""Container health-check CLI (the slim image has no curl).

python -m app.healthcheck http <url>          exit 0 if the URL returns 2xx
python -m app.healthcheck heartbeat <key> <max_age_seconds>
"""

import asyncio
import sys
import time
import urllib.request

from redis.asyncio import Redis

from app.config import get_settings


def check_http(url: str) -> bool:
    if not url.startswith(("http://", "https://")):
        return False
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310  (scheme checked above)
            return 200 <= int(resp.status) < 300
    except OSError:
        return False


async def check_heartbeat(key: str, max_age_seconds: float) -> bool:
    redis: Redis = Redis.from_url(get_settings().redis_url)
    try:
        value = await redis.get(key)
    finally:
        await redis.aclose()
    if value is None:
        return False
    return time.time() - float(value) <= max_age_seconds


def main(argv: list[str]) -> int:
    match argv:
        case ["http", url]:
            ok = check_http(url)
        case ["heartbeat", key, max_age]:
            ok = asyncio.run(check_heartbeat(key, float(max_age)))
        case _:
            print(__doc__, file=sys.stderr)
            return 2
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
