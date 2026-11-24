"""The scenario director's prompt: route facts, Chennai context and the rules for each field.

The LLM only describes *what happens* (rain, a surge, a breakdown); the simulator computes
every consequence (rule 2). Ridership figures give the model a sense of scale.
"""

from scenario_gen.checks import MAX_COMBINED_DEMAND, WEATHER_SPEED, ScenarioContext

# Public figures for scale (2025): MTC carries about 33-35 lakh passengers a day on roughly
# 3,500-4,100 buses (Wikipedia "List of bus routes in Chennai", Nov 2025; Down To Earth,
# "How India moves: Chennai", 2025; DT Next on the MTC business plan).
MTC_FACTS = (
    "Chennai's MTC carries about 33-35 lakh (3.3-3.5 million) passengers a day on roughly "
    "3,500-4,100 buses; buses are often overcrowded at peak hours and during the monsoon "
    "(October-December), when heavy rain and waterlogging slow traffic sharply."
)

# Landmarks near 32B stops (by stop name), so descriptions can be mapped to stops.
LANDMARKS = (
    "Chepauk cricket stadium (M. A. Chidambaram Stadium, IPL matches): nearest stops are "
    "'D1 Police Station', 'Adham Market' and 'Triplicane Post Office'. "
    "Hospitals: 'Stanley Hospital', 'Mgr Central Or Rajiv Gandhi Government General Hospital'. "
    "Chennai Central railway station: 'Mgr Central...' stops. Markets: 'Flower Bazzar', "
    "'Payaigada Evening Bazar'. Marina beach side: 'Vivekananda House', 'Ice House Police Station'."
)


def system_prompt(ctx: ScenarioContext) -> str:
    route = ctx.route
    lines = []
    for d in route.directions:
        stops = "; ".join(f"{s.stop_id} = {s.name}" for s in d.stops)
        lines.append(
            f"Direction {d.direction_id} (towards {d.headsign}): peak headway "
            f"{d.scheduled_headway_s // 60} min, trip {d.scheduled_run_s // 60} min, "
            f"{d.n_stops} stops in order: {stops}"
        )
    weather = "; ".join(f"{k}: {lo}-{hi}" for k, (lo, hi) in WEATHER_SPEED.items())
    directions = "\n".join(lines)
    return f"""You design realistic operating scenarios for a bus simulation of Chennai MTC route \
{route.route_id}. You return ONE JSON object that matches the given schema. Do not explain.

Context: {MTC_FACTS}
Landmarks: {LANDMARKS}
{directions}
The route has {ctx.fleet_size} buses, numbered 1-{ctx.fleet_size}.

Rules:
- start_time is the clock time (HH:MM, 24 h) the scenario starts; every *_min field is minutes \
from start_time and must lie within duration_min (30-180).
- demand_multiplier: 1.0 = a normal day at that hour (the time-of-day pattern is already \
modelled); festivals, events or strikes elsewhere raise it, holidays lower it.
- weather windows: kind and speed_factor must agree ({weather}); demand_factor > 1 if rain \
pushes people onto buses, < 1 if it keeps them home. Windows must not overlap.
- surges: extra riders at 1-4 stops of ONE direction (use that direction's stop ids), \
multiplier 1.2-6; give a short reason. A match crowd leaves in both directions: use one surge \
per direction.
- slowdowns: traffic between two stops of one direction (from_stop_id before to_stop_id), \
speed_factor 0.2-0.95, with a reason.
- breakdowns and gps_dropouts: leave both lists EMPTY unless the description explicitly \
mentions a breakdown or a GPS/tracking problem. Weather and crowds alone never cause them.
- Model only what the description says; a realistic scenario changes one or two things.
- Keep demand_multiplier x weather demand_factor x surge multiplier at or below \
{MAX_COMBINED_DEMAND:g}.
- Use only stop ids listed above. Leave lists empty when nothing of that kind happens."""


def user_prompt(description: str) -> str:
    return f"Scenario description: {description.strip()}"


def repair_prompt(problems: list[str]) -> str:
    listed = "\n".join(f"- {p}" for p in problems)
    return (
        "That scenario cannot be used. Fix these problems and return the complete JSON "
        f"object again:\n{listed}"
    )
