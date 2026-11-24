# VazhiSync — Progress

Names from `docs/build-plan.md`. Tick a module when its "Done when" test passes, with a one-line note.

**Backend order:** B1 → B3 → B2 → B4 → B6 → B5 → B7 → B8 → B10 (rules only) → B11 → B12 → B9 → B21 → B22 → B17 → B18 → B19.
**Frontend:** B13 → B14 → B15 → B16 → B23. **Last:** B24 (optional) → B20.

- [x] Session 0 — environment and scaffolding (uv, Python 3.12, Ollama + qwen3:8b; repo skeleton; GTFS zip downloaded)

## Phase A: Foundation
- [x] B1. Project setup and infrastructure — compose (PostGIS on host 5434, Redis, api, worker, avl-mcp, demand-mcp, sim-runner all healthy; Langfuse v3 behind `--profile observability`; native host Ollama), pydantic-settings config, async Alembic (0001 enables PostGIS), GitHub Actions CI (ruff, mypy, pytest). Acceptance: `uv run pytest -m stack`.
  - Follow-up: database image needs pgvector → B22.
- [x] B2. Static data: Chennai GTFS import — route **32B** Korukkupet ↔ Vivekananda House (21/22 stops, ~10 km, 5–6 min peak headway, passes Chepauk/Triplicane). Feed has no bus shapes, so geometry is OSRM-snapped once and committed (local detours repaired, OSM attribution). Importer validates order, stop-to-line offset (max 25 m), detours, spacing, headway, speed; idempotent writes; `GET /routes/{id}` returns GeoJSON. Migration 0003 adds routes/stops tables and FKs from B3 tables. Acceptance: `tests/integration/test_routes_api.py`.
  - Follow-up: per-stop-pair snapping, short-turn connection paths, per-segment road distance/duration, `waypoints.json`, `geometry_source` → B6 (done there).
- [x] B3. Database schema, login and roles — 17 tables (migration 0002; enums as VARCHAR+CHECK; unique `instruction_id`, one approval per recommendation, one open assignment per bus/driver), argon2 + JWT with role re-checked against the DB, `require_role` on every endpoint (enforced by test), register always creates a passenger, Redis rate limit on login/register, seed CLI for officers/drivers. Acceptance: `tests/integration/test_auth_roles.py`.
  - Follow-up: new migrations — predicted benefit, recommendation expiry, "can't comply" status → B12; predicted bunching-event kind → B21; pgvector + `cases` → B22.

## Phase B: The simulated world
- [x] B4. Simulation engine — pure-Python `simulator/` (no I/O, no `app` imports): 14 buses cycling 32B with layovers and no terminal headway control; Poisson demand with a time-of-day profile; dwell `10 + 2.5·board + 1.5·alight` with boarding during the dwell (the bunching feedback); capacity 80 / left-behind; log-normal travel noise; weather, slowdown, demand-surge and breakdown events; hold / skip-stop / short-turn validated against bus position; `step`, `snapshot`, `from_observations` (observable fields only, rule 1), `fast_forward`; named RNG streams per stop/bus (deterministic, common random numbers). Calibrated: trips 35.2/38.8 min vs timetable 35/37; 23/30 seeds first bunch in 15–30 min, all by 33. Acceptance: `tests/unit/test_simulator_acceptance.py`.
  - Follow-up (B6): running time is now spread over segments by OSRM road time; recalibrated to `run_share` 0.68 — trips 34.8/36.9 min vs 35/37, 26/30 seeds first bunch in 15–30 min, all by 32.
- [ ] B5. Scenario generator with a local LLM
- [x] B6. Live world runner and streaming — `app/runner/` ticks the world every 2 s (1×/5×/10×, set by officers via `PUT /officer/world/speed`, read from Redis each tick; whole simulated seconds so the seed stays deterministic); a GPS layer turns positions into lon/lat/heading on the snapped line and writes `vehicle_positions` (dropout buses write nothing, so their fixes age and go stale server-side) and `stop_queues` every 10 s, pruned after 6 h; bunching detected from reported fixes only (shared `app/live/headways.py`, terminal-zone buses excluded, stale pairs never classified), one open event per pair with hysteresis, carried across runner restarts, published on a Redis channel for B10; one `LiveFrame` per tick to Redis; `/ws/live` authenticates with a first-message token (never in the URL) and filters per role on the server (officer all, driver own bus + adjacent gaps, passenger public data without loads). Geometry follow-up from B2: per-stop-pair OSRM snap (41 pairs; outbound 2→3 flagged and drawn straight), per-segment duration and `geometry_source` (migration 0004), optional `waypoints.json`, 4 short-turn connections in `32B.turns.geojson`. Dev map: `backend/devtools/live_map.html`. Acceptance: `tests/integration/test_live_world.py`.

## Phase C: Tools (MCP)
- [ ] B7. MCP servers
- [ ] B8. MCP client and agentic harness (part 1)

## Phase D: The brain
- [ ] B9. LLM gateway and agentic harness (part 2)
- [ ] B10. Agent workflow with LangGraph
- [ ] B11. Counterfactual simulation workers

## Phase E: Human control
- [ ] B12. Approval, dispatch and driver feedback

## Phase F: Interfaces (React PWA)
- [ ] B13. Frontend shell and shared map
- [ ] B14. Officer console
- [ ] B15. Driver view
- [ ] B16. Passenger app

## Phase G: Hardening and proof
- [ ] B17. Reliability hardening
- [ ] B18. Observability and cost reporting
- [ ] B19. Evaluation harness
- [ ] B20. Documentation and submission evidence

## Phase H: Extensions
- [ ] B21. Early warning
- [ ] B22. Case retrieval
- [ ] B23. Tamil language
- [ ] B24. Jev decision tier (optional, build last)
