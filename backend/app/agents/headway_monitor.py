"""Headway Monitor: is the data fresh, and is the reported pair still bunched?

Uses avl-mcp only (its allowlist). Stale data ends the run with a flag — nobody is instructed
on stale GPS (rule 4); a pair that recovered or is no longer adjacent ends it too.
"""

from typing import Any

from app.graph.deps import WorkflowDeps
from app.graph.state import Snapshot, WorkflowState
from mcp_servers.schemas import HeadwayPair, Headways, VehiclePositions


def find_pair(headways: Headways, leader_id: str, follower_id: str) -> HeadwayPair | None:
    for pair in headways.pairs:
        if (
            str(pair.leader_vehicle_id) == leader_id
            and str(pair.follower_vehicle_id) == follower_id
        ):
            return pair
    return None


def decide(pair: HeadwayPair | None) -> str:
    if pair is None:
        return "not_bunched"  # overtaken, or one bus reached the terminal
    if pair.status == "stale" or pair.data_quality == "stale":
        return "stale"
    if pair.status != "bunched":
        return "not_bunched"
    return "generate"


def world_time(positions: VehiclePositions) -> float:
    times = [
        v.world_time_s for v in positions.vehicles if v.world_time_s is not None and not v.is_stale
    ]
    times = times or [v.world_time_s for v in positions.vehicles if v.world_time_s is not None]
    return max(times) if times else 0.0


async def headway_monitor(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    args = {"route_id": state["route_id"]}
    positions = await deps.tool("headway_monitor", "get_vehicle_positions", args, VehiclePositions)
    headways = await deps.tool("headway_monitor", "get_headways", args, Headways)
    pair = find_pair(headways, state["leader_vehicle_id"], state["follower_vehicle_id"])
    route = decide(pair)
    out: dict[str, Any] = {"route": route}
    if route == "stale":
        out["end_reason"] = "stale_data"
    elif route == "not_bunched":
        out["end_reason"] = "not_bunched"
    if pair is not None:
        out["snapshot"] = Snapshot(
            world_time_s=world_time(positions),
            positions=positions,
            headways=headways,
            estimates=None,
            pair=pair,
        )
    return out
