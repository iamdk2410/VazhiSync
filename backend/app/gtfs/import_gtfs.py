"""Import one route from the GTFS zip into Postgres (B2).

    uv run python -m app.gtfs.import_gtfs --route 32B            # validate + write
    uv run python -m app.gtfs.import_gtfs --route 32B --dry-run  # validate + report only

Exit code 0 on success, 1 if the route fails validation (nothing is written).
"""

import argparse
import asyncio
import sys
from pathlib import Path

from app.db.session import get_engine, get_sessionmaker
from app.gtfs.builder import RouteImport, RouteValidationError, build_route
from app.gtfs.feed import FeedError, GtfsZip
from app.gtfs.route_config import (
    ROUTES_DIR,
    CachedShape,
    ConfigError,
    RouteConfig,
    load_cached_shape,
    load_route_config,
)
from app.gtfs.writer import write_route


def prepare(
    route_id: str, feed_path: Path | None = None, routes_dir: Path = ROUTES_DIR
) -> RouteImport:
    """Load config, feed and cached shapes, and build + validate the route (no DB)."""
    config: RouteConfig = load_route_config(route_id, routes_dir)
    gtfs = GtfsZip(feed_path or config.feed_path)
    try:
        feed = gtfs.load({d.gtfs_route_id for d in config.directions})
    finally:
        gtfs.close()
    shapes: dict[int, CachedShape | None] = {
        d.direction_id: load_cached_shape(routes_dir / d.shape_file) for d in config.directions
    }
    return build_route(feed, config, shapes)


def report(route: RouteImport) -> str:
    lines = [f"Route {route.id} ({route.long_name}) - {route.agency}, feed {route.feed_version}"]
    for d in route.directions:
        lines.append(
            f"  dir {d.direction_id} -> {d.headsign}: {len(d.stops)} stops, "
            f"{d.length_m / 1000:.2f} km ({d.shape_source}), "
            f"run {d.scheduled_run_seconds // 60} min, "
            f"peak headway {d.peak_headway_seconds / 60:.1f} min, {d.trips_per_day} trips "
            f"{d.first_departure}-{d.last_departure}, "
            f"max stop offset {max(s.offset_from_shape_m for s in d.stops):.0f} m"
        )
    lines.extend(f"  warning: {w}" for w in route.warnings)
    return "\n".join(lines)


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.gtfs.import_gtfs", description=__doc__)
    parser.add_argument("--route", required=True, help="route config id, e.g. 32B")
    parser.add_argument("--feed", type=Path, help="GTFS zip (default: from the route config)")
    parser.add_argument("--dry-run", action="store_true", help="validate only, write nothing")
    args = parser.parse_args(argv)
    try:
        route = prepare(args.route, args.feed)
    except (ConfigError, FeedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except RouteValidationError as exc:
        print("validation failed, nothing written:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(report(route))
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    try:
        async with get_sessionmaker()() as session:
            await write_route(session, route)
    finally:
        await get_engine().dispose()
    print(f"imported route {route.id}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
