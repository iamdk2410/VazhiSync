# VazhiSync backend

Read the root `../CLAUDE.md` first: it has the project summary and the non-negotiable rules.
Everything Python lives in this folder: the API, the agents, the simulator and the MCP servers.

## Stack
- Python 3.12, `uv` for dependencies (one `pyproject.toml` in this folder), FastAPI, Pydantic v2, pydantic-settings
- Postgres + PostGIS, SQLAlchemy 2.0 (async, asyncpg), GeoAlchemy2, Alembic
- Redis (pub/sub + ARQ job queue)
- LangGraph (+ langgraph-checkpoint-postgres) for the agent workflow
- MCP: official `mcp` Python SDK (FastMCP, streamable HTTP); client via `langchain-mcp-adapters`
- LLMs: provider APIs for agents via LiteLLM; local Ollama for scenario generation and last-resort fallback
- Observability: structlog (JSON logs) + Langfuse
- Auth: PyJWT + pwdlib (argon2); rate limiting on public auth routes
- Tests: pytest, pytest-asyncio, httpx; lint: ruff; types: mypy

## Layout
```
backend/
├── pyproject.toml
├── alembic.ini
├── Dockerfile
├── simulator/      world model: step, snapshot, from_observations, fast_forward (pure Python, no I/O)
├── scenario_gen/   local-LLM scenario director (Ollama) + scenario library + golden scenarios
├── mcp_servers/
│   ├── avl_mcp/    get_vehicle_positions, get_headways, route://{id}/shape resource
│   └── demand_mcp/ get_boarding_estimates, send_driver_instruction
├── app/
│   ├── api/        FastAPI routers (auth, officer, driver, passenger, lab) + websocket; schemas.py
│   ├── auth/       password hashing + JWT (security), require_role (deps), rate limit, user service
│   ├── gtfs/       GTFS import: feed (validated rows), geometry, schedule, builder (validation), writer, snap_shape
│   ├── agents/     headway_monitor, action_generator, impact_simulator, control_advisor
│   ├── graph/      LangGraph state, node wiring, routing, checkpointer
│   ├── workers/    ARQ simulation jobs
│   ├── llm/        gateway: fallback chain, retries, token/cost logging
│   ├── db/         SQLAlchemy models, migrations, seed scripts
│   ├── observability/
│   ├── runner/     live world sim-runner process (B6; heartbeat stub for now)
│   ├── healthcheck.py  container health-check CLI (http / redis heartbeat)
│   └── config.py   pydantic-settings
├── docker/         compose support files (postgres-init SQL)
├── data/gtfs/      Chennai GTFS zip (not committed) + routes/<id>.json config and snapped GeoJSON (committed); see its README
└── tests/          unit/, integration/, edge_cases/
```
`simulator/` must not import from `app/` (it stays pure and reusable by the world runner,
the workers and the tests).

## Config (`../.env`, loaded via pydantic-settings)
`DATABASE_URL`, `REDIS_URL`, `JWT_SECRET`, `STALE_GPS_SECONDS=60`,
`BUNCHING_RATIO=0.5`, `CLOSING_RATIO=0.75`, `SIM_TIMEOUT_SECONDS`, `SIM_CONCURRENCY=4`,
`MAX_GRAPH_STEPS`, `PRIMARY_MODEL`, `BACKUP_MODEL`, `LOCAL_MODEL`, `OLLAMA_URL`,
`CORS_ORIGINS` (the frontend dev URL), provider API keys, Langfuse keys.

## Commands (run from `backend/`)
- Start services (from repo root): `docker compose up -d`
  (+ Langfuse: `docker compose --profile observability up -d`; Ollama runs natively on the host)
- Install: `uv sync`
- Migrations: `uv run alembic upgrade head`
- Run API locally: `uv run uvicorn app.main:app --reload`
- Tests: `uv run pytest` (fast unit tests: `uv run pytest tests/unit`).
  `services`-marked tests use Postgres + Redis from compose, on a separate `<db>_test`
  database that is created and migrated automatically (skipped if Postgres is down; CI sets
  `REQUIRE_SERVICES=1`). `stack` tests need the whole compose stack and are excluded by
  default: `uv run pytest -m stack`.
- Lint/format/types: `uv run ruff check . && uv run ruff format . && uv run mypy .`
- Seed users (officer/driver only): `SEED_PASSWORD=... uv run python -m app.db.seed --role officer --email ... [--name ...]`
  (driver: add `--vehicle <fleet_number>`; inside compose: `docker compose exec -e SEED_PASSWORD=... api python -m app.db.seed ...`)
- Import the route (needs the GTFS zip, see data/gtfs/README.md): `uv run python -m app.gtfs.import_gtfs --route 32B [--dry-run]`
- Re-snap route geometry (one-off, calls the public OSRM server): `uv run python -m app.gtfs.snap_shape --route 32B`
- Regenerate the simulator's 32B fixture after a re-import: `uv run python -m app.gtfs.sim_route --route 32B --out tests/fixtures/route_32b.json`
- New migration: `uv run alembic revision --autogenerate --rev-id 000N -m "..."`, review it, then `uv run alembic check`

## Conventions
- Small, typed, async functions; type hints everywhere; no bare `except`.
- API responses use Pydantic schemas that the frontend can generate types from (keep the
  OpenAPI schema clean: named models, no untyped dicts).
- Enable CORS only for `CORS_ORIGINS`.
