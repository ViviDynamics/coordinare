See AGENTS.md for all development guidelines.

## Active Technologies
- Python 3.11 (coordinare) + the existing performer image (Node + gh CLI already baked in) + `httpx` (already a transitive dep via the GitHub service), `pydantic` v2 for the rollup schema, structlog for observability (064-closer-pr-checks-gate)
- No new persistent state. Timeout anchor derives from the PR's HEAD-commit push timestamp (always re-derived from GitHub on each evaluation per FR-009). (064-closer-pr-checks-gate)
- Python 3.11 (coordinare) + LangGraph (existing), pydantic v2 (existing), structlog (existing) (066-unify-card-pickup)
- Existing JSON snapshot at `state_store.py`; schema unchanged. v1 snapshots must round-trip through unified code. (066-unify-card-pickup)
- Python 3.11 (coordinare + performer) (067-compatibility-first-backend)
- N/A — backend is stateless beyond opencode's per-session CLI state, identical to other adapters. (067-compatibility-first-backend)
- Python 3.11 (performer container; same runtime as existing backends) + `hermes-agent` (PyPI; baked into performer image — installation is out of scope per spec assumption), `asyncio.subprocess` for non-interactive CLI invocation, `pydantic` v2 (existing), `structlog` (existing) (068-hermes-backend)
- Job-scoped temp directory used as `HERMES_HOME` (created at `start()`, removed unconditionally on every terminal outcome per FR-011). No persistent state. (068-hermes-backend)
- Python 3.11 (coordinare) + LangGraph (existing), pydantic v2 (existing), structlog (existing) — no new deps (069-blocked-notification-rehydration)
- JSON snapshot via `state_store.py` (`WorkflowSnapshot` + `PersistedSession`). v1 and v2 snapshots must round-trip without loss; new `PersistedSession.last_blocked_notified_at` is optional and defaults to `None` for backward compatibility. (069-blocked-notification-rehydration)

## Recent Changes
- 066-unify-card-pickup: Single pickup path for any `max_concurrent_cards`. `active_sessions` is the source of truth; `state["current_card"]` is a derived mirror written only via `_set_current_card` / `_rederive_current_card` (FR-010). v1 snapshots rehydrate through the same unified code (FR-007).
- 064-closer-pr-checks-gate: Added Python 3.11 (coordinare) + the existing performer image (Node + gh CLI already baked in) + `httpx` (already a transitive dep via the GitHub service), `pydantic` v2 for the rollup schema, structlog for observability
