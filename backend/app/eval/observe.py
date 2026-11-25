"""What the advisor would observe of an evaluation world (B19): the MCP tools' answers.

The evaluation world is a simulation playing the role of reality. The advisor must only see
what avl-mcp and demand-mcp would report (rule 1): bus positions and loads as GPS/counters
report them, headways computed from those fixes by the detector's own rule, and waiting counts
at stops. These builders produce exactly those tool-result models (and nothing else), so the
agents' own functions can run on them unchanged.
"""

import math
import uuid
from datetime import UTC, datetime

from app.live.headways import DirectionInfo, Fix, compute_headways
from mcp_servers.schemas import (
    BoardingEstimates,
    HeadwayPair,
    Headways,
    StopEstimate,
    VehiclePosition,
    VehiclePositions,
)
from simulator import RouteModel, SimConfig, Simulation

_IDS: dict[str, uuid.UUID] = {}


def vehicle_id(fleet: str) -> uuid.UUID:
    return _IDS.setdefault(fleet, uuid.uuid5(uuid.NAMESPACE_DNS, f"eval.{fleet}"))


def directions_of(route: RouteModel) -> dict[int, DirectionInfo]:
    """The same per-direction headway inputs the route loader derives from the timetable."""
    return {
        d.direction_id: DirectionInfo(
            first_stop_m=d.stops[0].distance_m,
            scheduled_headway_s=float(d.scheduled_headway_s),
            avg_speed_mps=(d.stops[-1].distance_m - d.stops[0].distance_m)
            / max(d.stops[-1].scheduled_offset_s, 1),
        )
        for d in route.directions
    }


STALE_AFTER_S = 60.0


class Gps:
    """The GPS layer between the world and the advisor: each bus's last fix, frozen while its
    GPS is in a scenario's dropout window, with its age, so a silent bus turns stale exactly
    as avl-mcp would report it (rule 4: stale buses are never instructed)."""

    def __init__(self, dropouts: list[tuple[str, int, int]] | None = None) -> None:
        self._dropouts = dropouts or []  # (bus id, start s, duration s) from the world's start
        self._fixes: dict[str, tuple[int, float, float, int]] = {}  # bus -> dir, m, t, load
        self.now_s = 0.0

    def silent(self, bus: str, elapsed_s: float) -> bool:
        return any(b == bus and s <= elapsed_s < s + d for b, s, d in self._dropouts)

    def refresh(self, world: Simulation, elapsed_s: float) -> None:
        self.now_s = elapsed_s
        for b in world.snapshot().buses:
            if b.bus_id not in self._fixes or not self.silent(b.bus_id, elapsed_s):
                self._fixes[b.bus_id] = (b.direction, b.distance_m, elapsed_s, b.load)

    def age(self, bus: str) -> float:
        return self.now_s - self._fixes[bus][2]

    def fixes(self) -> list[Fix]:
        return [Fix(bus, d, m, self.age(bus)) for bus, (d, m, _, _) in self._fixes.items()]

    def positions(self, route_id: str, world_time_s: float) -> VehiclePositions:
        now = datetime.now(UTC)
        return VehiclePositions(
            route_id=route_id,
            server_time=now,
            stale_after_seconds=int(STALE_AFTER_S),
            missing=[],
            vehicles=[
                VehiclePosition(
                    vehicle_id=vehicle_id(bus),
                    fleet_number=bus,
                    direction_id=d,
                    lon=0.0,
                    lat=0.0,
                    distance_m=m,
                    speed_mps=None,
                    heading_deg=None,
                    load=load,
                    recorded_at=now,
                    world_time_s=world_time_s - self.age(bus),
                    age_seconds=self.age(bus),
                    is_stale=self.age(bus) > STALE_AFTER_S,
                )
                for bus, (d, m, _, load) in self._fixes.items()
            ],
        )

    def headways(
        self, route_id: str, dirs: dict[int, DirectionInfo], config: SimConfig
    ) -> Headways:
        pairs = [
            HeadwayPair(
                direction_id=h.direction_id,
                leader_vehicle_id=vehicle_id(h.leader_id),
                leader_fleet_number=h.leader_id,
                follower_vehicle_id=vehicle_id(h.follower_id),
                follower_fleet_number=h.follower_id,
                gap_m=h.gap_m,
                headway_seconds=h.headway_s,
                scheduled_headway_seconds=h.scheduled_headway_s,
                ratio=h.ratio,
                status=h.status,
                data_quality="stale" if h.status == "stale" else "ok",
                leader_age_seconds=self.age(h.leader_id),
                follower_age_seconds=self.age(h.follower_id),
            )
            for h in compute_headways(
                self.fixes(), dirs, STALE_AFTER_S, config.bunching_ratio, config.closing_ratio
            )
        ]
        return Headways(
            route_id=route_id,
            server_time=datetime.now(UTC),
            bunching_ratio=config.bunching_ratio,
            closing_ratio=config.closing_ratio,
            stale_after_seconds=int(STALE_AFTER_S),
            pairs=pairs,
            not_in_service=[],
            missing=[],
        )


def positions(world: Simulation, route_id: str) -> VehiclePositions:
    now = datetime.now(UTC)
    snap = world.snapshot()
    return VehiclePositions(
        route_id=route_id,
        server_time=now,
        stale_after_seconds=60,
        missing=[],
        vehicles=[
            VehiclePosition(
                vehicle_id=vehicle_id(b.bus_id),
                fleet_number=b.bus_id,
                direction_id=b.direction,
                lon=0.0,
                lat=0.0,
                distance_m=b.distance_m,
                speed_mps=None,
                heading_deg=None,
                load=b.load,
                recorded_at=now,
                world_time_s=snap.time_s,
                age_seconds=0.0,
                is_stale=False,
            )
            for b in snap.buses
        ],
    )


def headways(
    world: Simulation, route_id: str, dirs: dict[int, DirectionInfo], config: SimConfig
) -> Headways:
    fixes = [Fix(b.bus_id, b.direction, b.distance_m, 0.0) for b in world.snapshot().buses]
    pairs = [
        HeadwayPair(
            direction_id=h.direction_id,
            leader_vehicle_id=vehicle_id(h.leader_id),
            leader_fleet_number=h.leader_id,
            follower_vehicle_id=vehicle_id(h.follower_id),
            follower_fleet_number=h.follower_id,
            gap_m=h.gap_m,
            headway_seconds=h.headway_s,
            scheduled_headway_seconds=h.scheduled_headway_s,
            ratio=h.ratio,
            status=h.status,
            data_quality="ok",
            leader_age_seconds=0.0,
            follower_age_seconds=0.0,
        )
        for h in compute_headways(
            fixes, dirs, math.inf, config.bunching_ratio, config.closing_ratio
        )
    ]
    return Headways(
        route_id=route_id,
        server_time=datetime.now(UTC),
        bunching_ratio=config.bunching_ratio,
        closing_ratio=config.closing_ratio,
        stale_after_seconds=60,
        pairs=pairs,
        not_in_service=[],
        missing=[],
    )


def estimates(world: Simulation, route: RouteModel, horizon_min: int = 15) -> BoardingEstimates:
    """Waiting counts per stop (what the stop counters report), with a simple arrival-rate
    projection like demand-mcp's."""
    snap = world.snapshot()
    stops: list[StopEstimate] = []
    for d in route.directions:
        queue = snap.queues[d.direction_id]
        for i, stop in enumerate(d.stops[:-1]):
            waiting = int(queue[i])
            stops.append(
                StopEstimate(
                    direction_id=d.direction_id,
                    stop_id=stop.stop_id,
                    name=stop.name,
                    sequence=i + 1,
                    waiting_now=waiting,
                    arrivals_per_min=None,
                    expected_boardings=float(waiting),
                    sample_age_seconds=0.0,
                    data_quality="ok",
                )
            )
    return BoardingEstimates(
        route_id=route.route_id,
        server_time=datetime.now(UTC),
        horizon_minutes=horizon_min,
        stops=stops,
    )
