"""Headway Monitor: is the data fresh, and is the reported pair still bunched?

Uses avl-mcp only (its allowlist). Stale data ends the run with a flag — nobody is instructed
on stale GPS (rule 4); a pair that recovered or is no longer adjacent ends it too. A predicted
event (early warning, B21) goes on while the pair is adjacent and fresh, bunched or not yet.
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


def decide(pair: HeadwayPair | None, predicted: bool = False) -> str:
    """generate / stale / not_bunched. A predicted event (B21) acts on a pair that is not
    bunched yet: it only needs the pair to still be adjacent and its GPS fresh."""
    if pair is None:
        return "not_bunched"  # overtaken, or one bus reached the terminal
    if pair.status == "stale" or pair.data_quality == "stale":
        return "stale"
    if pair.status != "bunched" and not predicted:
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
    kind = state.get("event_kind", "actual")
    route = decide(pair, predicted=kind == "predicted")
    out: dict[str, Any] = {"route": route, "event_kind": kind}
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
