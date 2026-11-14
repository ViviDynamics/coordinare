# Pending Probe Recovery Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for implementation in this session. Independent agents review the change; they do not implement it.

**Goal:** Recover a cleaned, interrupted pending probe without resetting its attempt or reusing stale runtime evidence.

**Architecture:** Record abandoned reservations separately from observations, retain cumulative spend, and expose an explicit guarded recovery transition. Authorization failure is distinguished from executor failure at the existing service boundary.

**Tech Stack:** Existing Python, asyncio and Pydantic journal models; no new dependencies.

**Spec:** [pending-probe-recovery-amendment.md](pending-probe-recovery-amendment.md)

## Global constraints

- Preserve every consumed counter and the original expiry.
- Require confirmed cleanup and fresh runtime authorization.
- Do not migrate completed probe evidence or established manifests between images.
- Preserve version 9 readability and existing source-protocol restrictions.
- Use fictional identities in every public regression fixture.

## Review focus

- A late old executor must not complete an abandoned reservation.
- A resumed budget must include abandoned batches after serialization.
- A stale task must not remove or rewrite abandonment history.
- A callback cancellation must not start the executor or lose consumed spend.
- Recovery must refuse expired, exhausted, active, source-drifted or executable-bound attempts atomically.

## Task 1: Durable abandoned probe accounting

**Files:** `environment_probes.py`, `environment_attempt.py`, `environment_repair.py`; `tests/unit/test_353_pending_probe_recovery.py`.

- [x] Add failing synthetic tests for legacy/default roundtrips, two consumed batches including an abandonment, duplicate/tampered history and late report refusal.
- [x] Introduce the typed abandonment record and budget field; validate plan identities, append-only history and reservation accounting.
- [x] Add a guarded abandonment writer that synchronizes journal and budget without manufacturing a report.
- [x] Run the focused probe/journal tests and commit the coherent change.

## Task 2: Authorization interruption and explicit recovery

**Files:** `environment_probe_service.py`, `environment_repair.py`; the same synthetic regression module and existing pipeline tests.

- [x] First reproduce authorization timeout/cancellation leaving ownership set and preventing the next valid batch.
- [x] Abandon only before entering the executor; preserve ambiguous executor failure behavior.
- [x] Implement `recover_pending_probe(cache, *, expected_plan_digest, cleanup_confirmed, now=None)` with the amendment's guards.
- [x] Exercise recovery through production journal, service and discovery functions using a different synthetic image and fresh authorization. Assert unchanged history, source, counters and deadline.
- [x] Cover every refusal class and failed checkpoint before committing.

## Task 3: Independent review and release evidence

- [ ] Obtain adversarial review of the whole diff, including actual callback boundaries and legacy recovery; fix findings with regressions.
- [ ] Run Ruff, `scripts/ruff_baseline.py`, `mypy -p coordinare`, targeted 351/353 tests, `make test-all`, preflight 353 and fresh `--check 353` at the final head.
- [ ] Push to the existing child PR, wait for all checks through T3, and keep both discovery PRs open and unmerged.
- [ ] Use the recovery API only after fresh cleanup proof and only if the original attempt has not expired. Keep approval gates in active foreground waits.
