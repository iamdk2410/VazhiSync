"""Convert an imported GTFS route into the simulator's RouteModel (app -> simulator only).

    uv run python -m app.gtfs.sim_route --route 32B --out tests/fixtures/route_32b.json

The JSON is committed as a test fixture so simulator tests and CI don't need the GTFS zip.
"""

import argparse
import json
import sys
from pathlib import Path

from app.gtfs.builder import RouteImport
from app.gtfs.import_gtfs import prepare
from simulator.model import DirectionModel, RouteModel, StopModel


def to_route_model(route: RouteImport) -> RouteModel:
    directions = tuple(
        DirectionModel(
            direction_id=d.direction_id,
            headsign=d.headsign,
            length_m=round(d.length_m, 1),
            scheduled_headway_s=d.peak_headway_seconds,
            stops=tuple(
                StopModel(
                    stop_id=s.stop_id,
                    name=s.name,
                    lon=round(s.lon_lat[0], 6),
                    lat=round(s.lon_lat[1], 6),
                    distance_m=s.distance_along_m,
                    scheduled_offset_s=s.scheduled_offset_seconds,
                )
                for s in d.stops
            ),
        )
        for d in sorted(route.directions, key=lambda d: d.direction_id)
    )
    return RouteModel(route_id=route.id, directions=(directions[0], directions[1]))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.gtfs.sim_route", description=__doc__)
    parser.add_argument("--route", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    model = to_route_model(prepare(args.route))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(model.to_dict(), indent=1) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
