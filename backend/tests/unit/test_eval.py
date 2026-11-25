"""B19 evaluation code: observations, a short A/B play, the results table and charts."""

from typing import Any

from app.eval import observe
from app.eval.ab import play
from app.eval.charts import grouped_bars
from app.eval.run import reading, render, summarize
from scenario_gen.golden import load_golden
from simulator import SimConfig, Simulation
from tests.sim_fixtures import route_32b

ROUTE = route_32b()


def test_the_advisor_only_gets_tool_shaped_observations() -> None:
    world = Simulation.new(ROUTE, SimConfig(), 1)
    world.step(600)
    pos = observe.positions(world, "32B")
    hw = observe.headways(world, "32B", observe.directions_of(ROUTE), SimConfig())
    est = observe.estimates(world, ROUTE)
    assert {v.fleet_number for v in pos.vehicles} == {b.bus_id for b in world.snapshot().buses}
    assert all(not v.is_stale and v.lon == 0.0 for v in pos.vehicles)  # no coordinates leak
    assert hw.pairs and all(p.data_quality == "ok" for p in hw.pairs)
    assert est.stops and all(s.waiting_now is not None for s in est.stops)
    # stable ids: the same bus is the same vehicle across observations
    again = observe.positions(world, "32B")
    assert [v.vehicle_id for v in again.vehicles] == [v.vehicle_id for v in pos.vehicles]


def test_a_short_play_is_safe_and_measures_what_it_did() -> None:
    golden = next(g for g in load_golden() if g.key == "normal_morning_peak")
    control = play(golden, ROUTE, "control", golden.seed, max_minutes=20)
    advisor = play(golden, ROUTE, "advisor", golden.seed, max_minutes=20)
    assert control.interventions == [] and control.decisions == 0
    assert advisor.unsafe == 0 and advisor.invalid == 0
    for i in advisor.interventions:  # every applied action was measured against ground truth
        assert isinstance(i.actual_benefit, float) and i.action != "do_nothing"
    assert control.minutes == advisor.minutes == 20


def _row(scenario: str, arm: str, wait: float, cv: float, bunch: int) -> dict[str, Any]:
    return {
        "scenario": scenario, "arm": arm, "seed": 1, "minutes": 60,
        "passenger_wait_minutes": wait * 50, "avg_wait_s": wait, "headway_cv": cv,
        "bunching_count": bunch, "left_behind": 0, "decisions": 3, "do_nothing": 1,
        "interventions": [] if arm == "control" else [
            {"at_min": 5, "trigger": "bunched", "pair": "a>b", "action": "hold:b:60",
             "predicted_benefit": 30.0, "actual_benefit": 10.0},
        ],
        "unsafe": 0, "invalid": 0, "cpu_s": 1.0,
    }  # fmt: skip


def test_the_results_say_honestly_where_it_did_not_help() -> None:
    results = [
        _row("peak", "control", 200, 0.6, 20),
        _row("peak", "advisor", 170, 0.3, 3),
        _row("quiet", "control", 140, 0.1, 1),
        _row("quiet", "advisor", 150, 0.12, 1),
        _row("flat", "control", 100, 0.2, 2),
        _row("flat", "advisor", 100.5, 0.2, 2),
    ]
    summary = summarize(results)
    lines = reading(summary)
    assert any("peak" in line and "helped" in line and "-15%" in line for line in lines)
    assert any("quiet" in line and "did NOT help" in line and "worse" in line for line in lines)
    assert any("flat" in line and "no real difference" in line for line in lines)
    assert summary["prediction"] == {
        "mean_abs_error": 20.0, "predicted_total": 90.0, "actual_total": 30.0, "helped": 3,
    }  # fmt: skip
    md = render(summary, seeds=1, minutes=60)
    assert "| peak | Advisor | 170.0 | -15% |" in md and "did NOT help" in md


def test_charts_are_self_contained_svg() -> None:
    svg = grouped_bars(
        "Average wait", "seconds", ["a", "b"],
        {"control": [200.0, 140.0], "advisor": [170.0, None]},
    )  # fmt: skip
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert svg.count("<rect") >= 1 + 3 + 2  # background, three bars, legend
    assert "No control" in svg and "Advisor" in svg


def test_a_gps_dropout_freezes_the_bus_and_turns_its_pairs_stale() -> None:
    world = Simulation.new(ROUTE, SimConfig(), 1)
    world.step(600)
    bus = world.snapshot().buses[0].bus_id
    gps = observe.Gps([(bus, 600, 300)])  # silent from 10 to 15 minutes
    gps.refresh(world, 590)
    frozen = next(v for v in gps.positions("32B", 0).vehicles if v.fleet_number == bus)
    world.step(120)
    gps.refresh(world, 710)  # 2 minutes into the dropout
    now = next(v for v in gps.positions("32B", 0).vehicles if v.fleet_number == bus)
    assert now.distance_m == frozen.distance_m and now.is_stale and now.age_seconds == 120
    hw = gps.headways("32B", observe.directions_of(ROUTE), SimConfig())
    touching = [p for p in hw.pairs if bus in (p.leader_fleet_number, p.follower_fleet_number)]
    assert touching and all(p.status == "stale" for p in touching)  # rule 4: never acted on
    world.step(300)
    gps.refresh(world, 1010)  # the GPS is back
    back = next(v for v in gps.positions("32B", 0).vehicles if v.fleet_number == bus)
    assert not back.is_stale and back.age_seconds == 0
