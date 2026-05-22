# Quickstart: Hermes Performer Backend

**Feature**: 068-hermes-backend | **Date**: 2026-05-21

This is the documented smoke-test path required by FR-014 / SC-003. It
walks through the three-step verification: assessor first, architect
next, then a tiny implementer card — confirming no direct user messages
escape Hermes and all human-visible output appears on GitHub.

---

## Prerequisites

1. Performer container image with `hermes-agent` installed and a
   `hermes` CLI on `PATH`.
2. Coordinare `.env` populated with at least:
   ```env
   HERMES_PROVIDER=<provider id>
   HERMES_API_KEY=<credential>
   HERMES_MODEL=<model id>
   # Optional, for self-hosted endpoints:
   HERMES_BASE_URL=<base url>
   ```
3. A test symphony pointing at a low-risk repo (a sandbox repo with
   trivial `README` edits is sufficient).
4. The operator's `~/.hermes` directory: note its current state (file
   listing + mtime) before the test. It MUST be byte-identical
   afterwards.

## Configuration

Edit the symphony's `config.yaml` (or `config.example.yaml` clone) to
set:

```yaml
performers:
  default:
    backend: hermes
    model: <self-hosted-model-name>   # optional; falls back to HERMES_MODEL
```

Coordinare and the performer container restart picks up the new backend
identifier through the existing factory.

## Step 1 — Assessor role

1. Dispatch a card to the configured performer with role `assessor`.
2. Watch the performer logs for:
   - `Spawned hermes chat ... HERMES_HOME=/tmp/hermes-job-...`
   - Status transitions: `working` → `done`.
   - `Finalized: removed /tmp/hermes-job-...`
3. Confirm the assessor's verdict is posted as a GitHub issue comment
   by the performer wrapper.

## Step 2 — Architect role

1. Dispatch a card to the same performer with role `architect`.
2. Verify the architecture plan is posted as a GitHub PR/issue comment.
3. Confirm the architect's profile dir (a different tempdir than step
   1) is cleaned up.

## Step 3 — Implementer role (tiny change)

1. Dispatch an implementer card for a one-line README edit.
2. Verify:
   - Status reaches `done`.
   - The performer wrapper opens a PR through the existing GitHub
     service.
   - Coordinare observes `pr_opened` (status set unchanged — SC-006).

## Verification checklist

- [ ] **No direct user messages** were sent (check operator's
      messaging surfaces — chat, email, etc. — for any new traffic
      attributable to the Hermes runs).
- [ ] **`~/.hermes` untouched** (file listing + mtimes match the
      pre-test snapshot exactly).
- [ ] **All three job-scoped temp dirs removed** (`ls /tmp |
      grep hermes-job-` returns nothing).
- [ ] **Coordinare source tree unchanged**: `git diff origin/main --
      src/coordinare/` is empty (SC-006).
- [ ] **Status vocabulary clean**: no log line contains a Hermes-internal
      lifecycle string in the `state` field of `BackendStatus`.

## Failure-injection smoke (FR-010 / SC-005)

After the happy-path run, repeat step 3 with the model intentionally
mis-configured (e.g., `HERMES_MODEL` set to an unknown id). Expected:

- Status reaches terminal `error` within one job lifecycle.
- Profile dir is still cleaned up.
- Coordinare never sees an indefinite loop.

## Concurrency smoke (SC-004)

Dispatch two implementer cards back-to-back to the same performer with
`max_concurrent_cards >= 2`. Expected:

- Two distinct `hermes-job-*` directories appear in `/tmp` while both
  jobs are `working`.
- Neither job's profile dir is touched by the other.
- Both directories disappear after their respective terminal states.
