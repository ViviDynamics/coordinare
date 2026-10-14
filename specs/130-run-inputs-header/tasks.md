# Tasks: Run header with the inputs each run was given

Checked items are done and verified by running. Unchecked items are open, and each carries its reason.

- [x] T001 Spec, plan, tasks (this directory)
- [x] T002 Tests first: `tests/unit/test_130_run_header.py` (writer ordering, cursor recovery, redaction, failure containment, builder shape and bounds, real dispatch path, disabled payload, slow header, no-target gate)
- [x] T003 Tests first: `tests/unit/test_130_persona_history.py` (versions, cap, restart, torn line, dispatch reference, dashboard recording with local and OIDC actors, read API, auth gate, MCP deferral)
- [x] T004 `RunTranscriptWriter.stage_run_header` / `flush_run_header`; a staged header flushes before any content write (`src/coordinare/services/run_transcript.py`)
- [x] T005 `run_header.py`: `build_run_header`, `build_run_inputs`, bounds, null-with-reason
- [x] T006 `persona_history.py`: append-only per-role store, cap 200, registry, dispatch reference
- [x] T007 `HTTPPerformerService.dispatch_card(run_inputs=...)`: header written after acceptance, only when the target is known, bounded wait
- [x] T008 `dispatch_performer`: `run_inputs` for HTTP services; the persona language suffix shared as `PERSONA_LANGUAGE_SUFFIX`
- [x] T009 Personas router: versions recorded on save and reset; `GET /api/personas/{role}/history` and `.../history/{version}`; `app.state.oidc`
- [x] T010 MCP `DEFERRED_REST_PREFIXES` entry for the history routes
- [x] T011 mypy clean, ruff ratchet holds, ruff clean on touched files, the 130 and 129 suites pass
- [ ] T012 Whole test tree and quality guard (run after the commit)
- [ ] T013 Header for the four non-card dispatch sites (env bootstrap, wiki init, intake, documenting side). Open: those sites pass no transcript target and no persona reference. Reason: they need their own run inputs, which is a separate change (see plan D8).
