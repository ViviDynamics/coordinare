# Reviewer-scoped Source Conflict Correction Implementation Plan

> For agentic workers: use superpowers:executing-plans for inline TDD implementation.

**Goal:** Correct independently identified source conflicts without unfreezing unrelated requirements.

**Architecture:** Derive reassessment authority from the latest typed source assessment and
its current ledger binding. Pass that authority separately from existing network releases
through single and sharded revision validation, preservation, and author context.

**Tech stack:** Python 3.14, Pydantic, pytest, existing source-review protocol.

**Spec:** `source-conflict-amendment.md`, approved by the maintainer on 2026-10-26.

## Global constraints

No private repository identities in public fixtures. No model-specific rules. Preserve
all review history, independent acceptance, budgets, execution gates, and cleanup.

## Review focus

- Latest typed decisions supersede old conflicts and cannot leave permanent authority.
- Stale or mismatched review digests and source hashes fail closed.
- Required criteria outside the exact reviewer and shard assignment stay frozen.
- Corrected criteria remain unaccepted until a fresh reviewer closes the finding.
- Restart and multi-group combination preserve identities, history, and existing limits.

## Task 1: Current-review authority and revision integration

Files: create `src/coordinare/services/environment_scope_reopening.py` and
`tests/unit/test_353_scope_conflict_reopening.py`; modify `environment_scope.py`,
`environment_shard_scope.py`, `environment_repair.py`, and the inference package's
`environment/scope_revision.py`.

- [x] Write synthetic regression tests for new/retained typed conflicts, corrected required
  and not-applicable outcomes, unrelated freezes, supersession, identity failure, and restart.
- [x] Run tests and record failures against the unchanged implementation.
- [x] Implement `source_conflict_reopenings(budget, definition, *, finding_ids=None)` returning
  exactly the current independently named criterion IDs after provenance validation.
- [x] Add a distinct `reassessable` argument to revision application/combination. Pass exact
  authority through single/sharded validation, preservation, context, and ownership routing.
- [x] Run focused and all environment tests. Verify fresh review remains necessary.

## Task 2: Review and delivery

- [x] Request independent adversarial review; address actionable findings with regression tests.
- [ ] Run lint, Ruff baseline, mypy, full `make test-all`, preflight 353, and fresh --check.
- [ ] Commit and push only with passing evidence. Keep PR354/355 open and unmerged.
- [ ] Wait for current-head green CI before preparing a separately reviewed validation run.
