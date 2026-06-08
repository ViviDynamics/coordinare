# Phase 0 Research: Security Scan Gate

All Technical Context items resolved — no NEEDS CLARIFICATION remain. The design doc
(`docs/superpowers/specs/2026-06-07-security-scan-gate-design.md`) already settled the
architecture; this file records the concrete tool-invocation and normalization decisions.

## Decision 1: Static-analysis tools — semgrep + bandit

- **Decision**: Run semgrep (`semgrep --config auto --json`) and bandit (`bandit -f json -r
  <paths>`) as subprocesses over the PR's changed files; union the findings.
- **Rationale**: semgrep `--config auto` is multi-language (covers JS/TS/Python/Go/etc.) and
  emits structured JSON with severity + CWE metadata; bandit is Python-specialized and catches
  Python-specific issues semgrep's auto config can miss. Both are pip-installable, run offline
  once rules are cached, and already align with the "advisory tool in `Dockerfile.full`" plan.
- **Alternatives considered**:
  - *CodeQL* — rejected: heavyweight, requires a build/database step, too slow for an
    inline dispatch-time gate.
  - *semgrep alone* — rejected: bandit adds Python depth cheaply; union maximizes recall for
    the load-bearing floor.
  - *Custom regex rules* — rejected: reinvents a worse semgrep; high false-negative risk.

## Decision 2: Severity normalization → finding schema

- **Decision**: Map tool severities into the spec-022 finding schema fields (`severity`,
  `category`, `description`, `file`, `line`, `routing`):
  - semgrep `extra.severity`: `ERROR`/`CRITICAL` → `critical`, `WARNING` → `high` when CWE
    is in the dangerous set else `medium`, `INFO` → `low`. (Concrete mapping table in
    data-model.md; the floor only triggers on `critical`/`high`.)
  - bandit `issue_severity` × `issue_confidence`: `HIGH`/`HIGH` → `critical`, `HIGH`/`MEDIUM`
    or `MEDIUM`/`HIGH` → `high`, `MEDIUM`/* → `medium`, `LOW`/* → `low`.
  - `category` ← the rule's CWE / `check_id` (e.g. `injection`, `hardcoded_secret`).
  - `file`/`line` ← tool location fields.
  - `routing` ← default `implementer` (FR-002); the model may re-route in its own findings.
- **Rationale**: Reuses the existing finding schema (FR-012, no protocol change) so findings
  flow through `relay_feedback` and `card_context` unchanged. The floor's critical/high
  threshold is the only severity logic that gates the verdict.
- **Alternatives considered**: passing raw tool JSON through — rejected: breaks the shared
  finding contract and the `card_context`/`relay_feedback` consumers.

## Decision 3: Diff acquisition — `get_pr_diff(pr_url)`

- **Decision**: Extend `src/coordinare/services/github.py` with `get_pr_diff(pr_url) ->
  (raw_diff, changed_files)`. Coordinare already holds the GH token; use `gh pr diff` /
  GitHub API to fetch the unified diff and the changed-file list.
- **Rationale**: The coordinare leg is authoritative and already authenticated. `changed_files`
  scopes the scan; `raw_diff` is available if a diff-level scan mode is needed. Keeps all GH
  access in the existing helper module (`github.py` already has `get_pr_files`:989,
  `get_pr_reviews`:1330).
- **Alternatives considered**: scanning the whole repo checkout — rejected: slow and noisy;
  the gate is about the PR's *changes*. Re-cloning — rejected: coordinare already has repo access.

## Decision 4: Run-once, reuse (single scan per dispatch)

- **Decision**: Scan exactly once in `dispatch_performer.py` when `role == "security"`; stash
  in `state["scanner_findings"]`. Both the `card_context` injection (dispatch) and the floor
  enforcement (monitor) read the **same** stashed list.
- **Rationale**: FR-006. Avoids a second scan at verdict time; deterministic and cheaper.
- **Alternatives considered**: re-scanning at monitor time — rejected: wasteful, risks
  divergence between what the model saw and what the floor enforces.

## Decision 5: Fail-closed semantics

- **Decision**: Wrap scan + diff-fetch in error handling. On any failure (scanner missing,
  crash, timeout, diff-fetch error), stash a single synthetic finding
  `{severity: critical, category: scanner_unavailable, routing: halt}` and let the monitor
  floor force `security_failed`. Emit a loud observability marker
  (`monitor_performer.scanner_unavailable` / dispatch-side equivalent).
- **Rationale**: FR-008. An absent floor is the exact 082 failure mode; never silently absent.
  `routing: halt` (not `implementer`) avoids a pointless fix-loop — an implementer cannot fix
  broken tooling.
- **Alternatives considered**: fail-open (proceed on scanner error) — rejected outright,
  reintroduces the single-model-no-floor risk. Retry-then-fail — deferred (YAGNI); a timeout
  budget + fail-closed is sufficient for MVP.

## Decision 6: Performer advisory tool vs coordinare floor

- **Decision**: Install semgrep + bandit in `agent/performer/Dockerfile.full` and expose as a
  performer tool the model may invoke live; these feed the model's own `findings[]` but do
  **not** bind the verdict. Only the coordinare leg's stashed findings gate `security_failed`.
- **Rationale**: FR-007. Separates ceiling (advisory, model-driven) from floor (authoritative,
  coordinare-driven). `Dockerfile.full` already carries ruff/black/shellcheck/eslint — same
  layer, so the `Dockerfile.base` source-COPY invariant is untouched.
- **Alternatives considered**: making the performer tool authoritative — rejected: the model
  could skip or misread it; the floor must be coordinare-owned.

## Decision 7: Denylist location & scope (P3)

- **Decision**: Add a load-time `model_validator` in `src/coordinare/config.py` that resolves
  the `security` role's effective model and raises if it is in the denylist {`qwen3.6:35b`,
  `qwq:32b`, `qwen2.5:14b-instruct`, `qwen2.5:32b`, `qwen3-coder:30b`}. Scope: `security` role
  only.
- **Rationale**: FR-010. Codifies the production lever (security must run on a capable judge);
  a future config edit cannot silently bind security to a known-weak model. Reuses the
  existing `resolved_role(role_name)` resolution (config.py:550) and the existing validator
  pattern (lines 110/141/149/352/401/489).
- **Alternatives considered**: runtime check at dispatch — rejected: fails late, after a job
  starts; load-time fails fast. Applying denylist to reviewer/assessor — out of scope per spec
  Non-Goals (082 finding is `security`-specific).

## Open items

None. All FR-001..FR-012 are covered by concrete decisions above.

## Tool versions (dev/unit-test environment)

Installed into `.venv` for unit tests (T001):
- `semgrep==1.165.0`
- `bandit==1.9.4`

The performer advisory copy (`Dockerfile.full`, T016) installs the same tools; pin to a
compatible range when the image is built.
