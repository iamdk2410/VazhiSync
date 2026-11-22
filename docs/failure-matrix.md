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
