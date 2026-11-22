# VazhiSync — Failure Matrix

One row per failure case. Add rows as each module is built.

| Failure | Detected by | Response | Test |
|---|---|---|---|
| Required config missing or invalid (`DATABASE_URL`, `JWT_SECRET` < 16 chars, out-of-range thresholds, `BUNCHING_RATIO ≥ CLOSING_RATIO`) | pydantic-settings validation at startup | Service refuses to start with a clear validation error; secrets never printed | `tests/unit/test_config.py` |
| Postgres or Redis down / unreachable | `GET /health` dependency checks (2 s timeout each) | `/health` returns 503 with per-check status; container marked unhealthy | `tests/unit/test_health.py` |
| A dependency check hangs | `asyncio.wait_for` timeout in `/health` | Reported as `error` instead of blocking the endpoint | `tests/unit/test_health.py::test_health_check_times_out_instead_of_hanging` |
| sim-runner stalls or crashes | Redis heartbeat older than 20 s (container healthcheck) | Container marked unhealthy; `restart: unless-stopped` restarts it on exit | `tests/integration/test_stack_health.py` |
| ARQ worker loses Redis / stops polling | `arq --check` health key | Container marked unhealthy | `tests/integration/test_stack_health.py` |
| MCP request with an unexpected `Host` header (DNS rebinding) | MCP transport security allowed-hosts list | Request rejected | — (B7) |
