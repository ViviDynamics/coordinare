# Plan: Run header with the inputs each run was given

## Decisions

- **D1 Writer** (`src/coordinare/services/run_transcript.py`): `stage_run_header(header)` holds the header in memory (no I/O). `flush_run_header()` writes it as the first line, redacted with `redact_value` and the run's exact secrets, counted in `_bytes`, and never subject to the cap drop. Every content write flushes a staged header first, under the writer lock, so ordering holds even when the dispatch-side flush timed out. A file that already has content is never given a header (`run_transcript.header_skipped`).
- **D2 Header builder** (`src/coordinare/services/run_header.py`, pure): `build_run_inputs` runs in the dispatch node and resolves what only the daemon knows (persona version, routing rule). `build_run_header` runs in `HTTPPerformerService` after acceptance and reads the card context and endpoint. Both return plain JSON and never raise.
- **D3 Transport** (`HTTPPerformerService`): `dispatch_card(..., run_inputs=None)` is a new keyword. The payload is not changed by it, so disabled transcripts keep the payload byte-identical. The header is written only when the run is transcribed. The write is `asyncio.to_thread(flush)` inside `asyncio.wait_for(RUN_HEADER_WAIT_SECONDS)`. Any failure is logged and dispatch returns normally.
- **D4 Persona history** (`src/coordinare/services/persona_history.py`): one JSONL file per role at `<state directory>/persona_history/<role>.jsonl`, next to the state snapshot (writable in Kubernetes, unlike the config). Append with fsync, a torn last line is closed before the next append, the cap prunes by atomic rewrite. The latest version per role is cached in memory after the first read. The stored text is the **effective** persona text (what `get_effective_instructions` returns, without the dispatch-time language suffix).
- **D5 Recording points**: the dashboard `PUT`/`DELETE /api/personas/{role}` record after the successful save (actor from the OIDC session, else `local`). The dispatch node records any difference from the latest version as `unknown` (FR-009).
- **D6 Routes**: read-only, in `src/coordinare/dashboard/routers/personas.py`. Auth comes from the global dashboard middleware. The prefix is in `DEFERRED_REST_PREFIXES`.
- **D7 Why the prompt is not in the header**: the rendered task prompt is built by each backend in the performer. The header states that and points at the run's first `user` record. Backends that do not emit that record already declare it in their `capability` record.
- **D8 Scope of dispatch sites**: the header is written for card runs, which dispatch through `dispatch_performer`. The four other `dispatch_card` callers (env bootstrap and wiki init in `daemon.py`, intake in `daemon.py`, and the documenting side in `services/documenting_side.py`) pass no `run_inputs` and no transcript target, so they write no header in this change. `ResilientAgentService` wraps only the legacy `AgentService`, not the HTTP performer services.
- **D9 Target gate**: a header is written only when the run's transcript target is known. Without it, a writer created at dispatch would name the file before the first poll knows the symphony (found by spec 129's own tests).

## Files

- `src/coordinare/services/run_transcript.py`: stage and flush the header; flush before content writes.
- `src/coordinare/services/run_header.py` (new): header and run-input builders, bounds.
- `src/coordinare/services/persona_history.py` (new): store, cap, registry, reference.
- `src/coordinare/services/http_performer_service.py`: `run_inputs` keyword, header write after acceptance.
- `src/coordinare/graph/nodes/dispatch_performer.py`: `run_inputs` kwarg for HTTP services.
- `src/coordinare/dashboard/routers/personas.py`: record on save and reset, history routes.
- `src/coordinare/dashboard/app.py`: `app.state.oidc` so the router can name the session user.
- `src/coordinare/mcp/tools/_registry.py`: DEFERRED prefix for the history routes.

## Verification

Unit tests `tests/unit/test_130_*.py` exercise the real writer, the real `HTTPPerformerService.dispatch_card` with an injected fake client, the real persona router under the real dashboard app, and the real `build_run_inputs` against a real `ProjectConfiguration`.
