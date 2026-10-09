# Cancel workers when the board pauses a card

Issue #552

## Scope
In: stop owned main and side workers on Backlog/Todo moves, retain uncertain ownership, resume without overlap, preserve instructions across restart.
Out: changing operator cancel semantics unrelated to board pause.

## Assumptions
- Board pauses cancel work; unknown termination retains a blocked session.
- Todo resumes only after all owned writers are confirmed stopped.
- Durable pause intent must survive startup phase reconciliation.

## Tasks
- [x] Add regressions for active pauses, uncertain termination, restart, resume, feedback and sibling isolation.
- [x] Verify HTTP worker termination before forgetting ownership.
- [x] Reconcile paused sessions before fanout and prevent fallback dispatch.
- [x] Run focused regression suites, lint and typecheck.

## Validation

- Regression suite: 275 passed before the two Docker-specific checks; all 17 issue checks pass afterward.
- Ruff: full src/ and tests/ clean.
- Mypy: no issues in 205 source files.
- Full preflight deferred to root after independent review, as instructed.

## Review corrections

- Pause intent and its source board column now participate in the real snapshot save gate.
- Running workers moved to Backlog/Todo during downtime stay in startup runtime reconciliation until confirmed stopped.
- Paused fallback runs board maintenance and sibling admission without invoking the worker graph; pause ownership is excluded from stale recovery and rebase writes.
- Moving active work to Todo cancels and remains paused while Todo is unchanged, including across restart. Move to Backlog and then Todo (or into an active column) to explicitly resume.
- Ruff debt ratchet holds without baseline changes; helper extraction removes new complexity violations.

Review validation: 492 focused and adjacent tests pass, including all 25 issue regressions; full Ruff passes; strict mypy passes for 205 modules; Ruff ratchet holds (PLC0415 count lowered from 497 to 495). Full preflight remains root-owned.

## Second review correction

Daemon preflight rebases before pause cancellation. The shared stale-branch/rebase-round selector now refuses persisted pause intent and accepts the freshly polled board snapshot, excluding Backlog/Todo before first pause detection. Regression tests run actual daemon preflight, preserve worker identity and queued/in-flight instruction provenance, and verify active siblings still rebase.

Validation: 208 relevant daemon/rebase tests pass (all 30 issue regressions included); full Ruff, strict mypy for 205 modules and the Ruff debt ratchet pass. No full preflight or push performed.
