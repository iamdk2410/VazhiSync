# VazhiSync — Failure Matrix

One row per failure case. Add rows as each module is built.

| Failure | Detected by | Response | Test |
|---|---|---|---|
| Required config missing or invalid (`DATABASE_URL`, `JWT_SECRET` < 32 chars, out-of-range thresholds, `BUNCHING_RATIO ≥ CLOSING_RATIO`) | pydantic-settings validation at startup | Service refuses to start with a clear validation error; secrets never printed | `tests/unit/test_config.py` |
| Postgres or Redis down / unreachable | `GET /health` dependency checks (2 s timeout each) | `/health` returns 503 with per-check status; container marked unhealthy | `tests/unit/test_health.py` |
| A dependency check hangs | `asyncio.wait_for` timeout in `/health` | Reported as `error` instead of blocking the endpoint | `tests/unit/test_health.py::test_health_check_times_out_instead_of_hanging` |
| sim-runner stalls or crashes | Redis heartbeat older than 20 s (container healthcheck) | Container marked unhealthy; `restart: unless-stopped` restarts it on exit | `tests/integration/test_stack_health.py` |
| ARQ worker loses Redis / stops polling | `arq --check` health key | Container marked unhealthy | `tests/integration/test_stack_health.py` |
| MCP request with an unexpected `Host` header (DNS rebinding) | MCP transport security allowed-hosts list | Request rejected | — (B7) |
| Missing, malformed, expired, tampered or `alg: none` JWT | `decode_access_token` (PyJWT, HS256 only, required claims, Pydantic payload) | 401 | `tests/unit/test_security.py` |
| Token's role no longer matches the database (role changed, user deactivated) | `get_current_user` compares token role with DB row | 401; old token unusable | `test_auth_roles.py::test_token_is_rejected_after_role_change` |
| User calls another role's endpoint | `require_role()` dependency | 403, logged | `test_auth_roles.py::test_role_reaches_own_endpoint_and_is_forbidden_elsewhere` |
| New endpoint added without `require_role` | Test calls every OpenAPI operation without a token | Test fails unless the route is on the explicit public list | `tests/unit/test_route_guards.py` |
| Registration tries to set a role / extra fields | `RegisterRequest` has no role field, `extra="forbid"` | 422, nothing created | `test_auth_roles.py::test_register_cannot_choose_a_role` |
| Brute-force login / mass registration | Redis fixed-window counter per client IP | 429 with `Retry-After` | `test_auth_roles.py::test_login_is_rate_limited`, `test_rate_limiter.py` |
| Redis down while rate limiting | `RedisError` in limiter | Fail open: request allowed, warning logged (an outage must not lock everyone out) | `test_rate_limiter.py::test_redis_outage_fails_open` |
| Email enumeration on login (message or timing) | Same 401 body for unknown email and wrong password; dummy argon2 check for unknown emails | Indistinguishable responses | `test_auth_roles.py::test_wrong_password_and_unknown_email_look_the_same` |
| Seeded account unusable because login validation is stricter than seeding | Login accepts any bounded string; seed applies register's email rules | Every stored account can log in | `test_auth_roles.py::test_login_never_rejects_an_existing_address_format`, `test_seed_rejects_emails_that_register_would_reject` |
| Duplicate driver instruction (same idempotency key) | `UNIQUE (instruction_id)` | Second insert rejected by the database | `test_schema_guarantees.py::test_instruction_id_is_an_idempotency_key` |
| Double-clicked Approve | `UNIQUE (recommendation_id)` on approvals | Second decision rejected by the database | `test_schema_guarantees.py::test_one_decision_per_recommendation` |
| Invalid status/enum value written directly to the DB | CHECK constraints on every enum column | Write rejected | `test_schema_guarantees.py::test_unknown_enum_value_rejected_by_database` |
| Models and migrations drift apart | `alembic check` in CI | CI fails | CI step "Migrations match the models" |
| Database image adds extra schemas (postgis image installs `topology`/`tiger` and extends `search_path`), so drift checks see foreign tables | `alembic check` in CI against a fresh `postgis/postgis` container | Alembic connects with `search_path=public`; only our schema is compared | CI step "Migrations match the models" (reproduced locally on a fresh container) |
| GTFS row invalid (bad id, name too long, coordinates outside Chennai, malformed time) | Pydantic row models in `app/gtfs/feed.py` | Import aborts with the file and field; nothing written | `test_gtfs_import.py::test_coordinates_outside_chennai_are_rejected`, `test_gtfs_geometry_schedule.py::test_parse_gtfs_time_rejects_garbage` |
| Hostile text in the feed (`<script>` in a field) | Treated as data: validated length, stored as plain text, JSON API | Never interpreted as markup (frontend must render as text) | `test_gtfs_import.py::test_markup_in_names_is_kept_as_inert_text` |
| Feed lists the same departure twice | `unique_sorted` before headway maths | No zero-minute headways | `test_gtfs_geometry_schedule.py::test_headway_profile_ignores_duplicate_departures` |
| Route stops out of order / stop far from the line / line starts or ends away from the terminals | `validate_direction` | Import fails listing every problem; nothing written | `test_gtfs_import.py` (validation tests) |
| Car routing adds a local detour (one-way / U-turn artefact) | Per-segment road vs straight ratio > 2.5 | Validation fails; `snap_shape` re-snaps the stretch pair-wise or uses a straight line, recorded in `patched_segments` | `test_gtfs_import.py::test_local_detour_fails_validation` |
| Cached shape stale (stop pattern changed) or for a different route | Stop ids / GTFS route id stored in the GeoJSON | Stale: fall back to stop-to-stop with a warning. Wrong route: import fails | `test_stale_cached_shape_falls_back_with_a_warning`, `test_cached_shape_for_another_route_is_rejected` |
| Route config id used for path traversal (`../`) | Route id pattern | Rejected before touching the filesystem | `test_route_config_ids_cannot_escape_the_routes_folder` |
| Re-import while vehicles/events reference the route | Route row upserted, only directions replaced, in one transaction | Foreign keys stay valid; partial imports impossible | `test_routes_api.py::test_reimport_replaces_directions_and_keeps_vehicles` |
| OSRM server slow/unavailable during the one-off snap | Timeout 30 s, 3 attempts on 429/5xx/connection errors only | Clear error; import still works from the existing cache or stop-to-stop | — (one-off tool) |
| Action invalid for the bus's position (skip/turn behind the bus or at the terminal, unknown or broken-down bus, hold out of bounds, no stop to turn at) | Validation in `Simulation.apply` and action constructors | `ActionError`; nothing applied (the guarded send tool in B7 repeats these checks) | `test_simulator_mechanics.py::test_invalid_actions_are_rejected`, `test_short_turn_needs_a_nearby_opposite_stop` |
| Observations malformed (no vehicles, duplicate ids, bad direction, load above capacity, negative queue) | `from_observations` | `ObservationError`; no what-if model built | `test_simulator_mechanics.py::test_bad_observations_are_rejected` |
| What-if model accidentally uses world ground truth | `from_observations` accepts only observable fields; destinations are spread by a typical profile | Rule 1 holds by construction | `test_from_observations_uses_only_what_was_observed` |
| Non-reproducible simulation (shared RNG, ordering) | Named per-stop/per-bus random streams from the seed | Same seed, same result; an action on one bus doesn't reshuffle other draws | `test_simulator_acceptance.py::test_same_seed_same_result`, `test_what_if_runs_are_reproducible` |
| What-if run mutates the live world | `fast_forward` deep-copies before applying | World unchanged | `test_fast_forward_leaves_the_original_untouched` |
| Simulator drifts away from the timetable after a change | Calibration guard test | Test fails if median trips deviate >15 % from GTFS run times | `test_simulated_trip_times_match_the_timetable` |
| Test passes locally only because a local default matches a hard-coded value (e.g. JWT secret) | Running the suite with CI's environment | Tests read the configured value, never a copy of the default | `tests/unit/test_security.py` |
