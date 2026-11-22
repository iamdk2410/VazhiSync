# VazhiSync — Progress

Names from `docs/build-plan.md`. Tick a module when its "Done when" test passes, with a one-line note.

**Backend order:** B1 → B3 → B2 → B4 → B6 → B5 → B7 → B8 → B10 (rules only) → B11 → B12 → B9 → B17 → B18 → B19.
Frontend (B13–B16) after the backend. B20 last.

- [x] Session 0 — environment and scaffolding (uv, Python 3.12, Ollama + qwen3:8b; repo skeleton; GTFS zip downloaded)

## Phase A: Foundation
- [x] B1. Project setup and infrastructure — compose (PostGIS on host 5434, Redis, api, worker, avl-mcp, demand-mcp, sim-runner all healthy; Langfuse v3 behind `--profile observability`; native host Ollama), pydantic-settings config, async Alembic (0001 enables PostGIS), GitHub Actions CI (ruff, mypy, pytest). Acceptance: `uv run pytest -m integration`.
- [ ] B2. Static data: Chennai GTFS import
- [ ] B3. Database schema, login and roles

## Phase B: The simulated world
- [ ] B4. Simulation engine
- [ ] B5. Scenario generator with a local LLM
- [ ] B6. Live world runner and streaming

## Phase C: Tools (MCP)
- [ ] B7. MCP servers
- [ ] B8. MCP client and tool layer

## Phase D: The brain
- [ ] B9. LLM gateway
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
- [ ] B19. Testing and evaluation
- [ ] B20. Documentation and submission evidence
