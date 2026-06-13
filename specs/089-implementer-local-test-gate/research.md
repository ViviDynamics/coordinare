# Phase 0 Research: Implementer Local Test Gate

All Technical Context unknowns are resolved below. No `NEEDS CLARIFICATION` remain.

---

## R1 — Where does the test step run, and how is it factored?

**Decision**: Add a `_run_test_check(stand_path, label)` helper in `agent/performer/src/performer/main.py`, directly mirroring the existing `_run_ci_check` (lines 100–135). It imports `coordinare.services.ci_detection.detect`, calls it once, and runs `result.test_command` (not `lint_command`) via the existing `run_command` with the configured timeout. It returns a structured `LocalTestResult` instead of `_run_ci_check`'s `(bool, str)` tuple, because the test gate has a third outcome (env-blocked) the lint gate does not.

**Rationale**: `detect()` already returns `test_command` for every stack it knows (e.g. `bundle exec rspec`, `pytest`); the data is on the table and simply ignored today. Mirroring `_run_ci_check` keeps the two gates structurally identical and easy to review. A dedicated helper (FR-001) satisfies the "reusable, role-agnostic" scope decision and keeps the done-path readable (Constitution I).

**Alternatives considered**:
- *Extend `_run_ci_check` to also run tests* — rejected: conflates style and correctness gates, muddies the `(bool, str)` contract, and forces the lint-only callers (other roles) to carry test logic.
- *Run tests inside `ci_detection`* — rejected: `ci_detection` is a pure detector; executing commands there violates single-responsibility.

---

## R2 — How is the env-blocked outcome signalled from a *pre-push* failure?

**Decision**: Introduce a **new role-agnostic terminal status `env_blocked`** in `protocol.py`, routed by `monitor_performer.py` exactly like the existing `qa_env_blocked` (hold on the SAME stage, call `mark_runtime_health_failed`, set `env_health_hold_reason`, `phase = "dispatching"`, return without advancing — lines 2076–2102). The local test gate, on detecting a code-reason test failure, calls `consume_services_start_failure()` / `consume_env_cache_health_failure()`; if either fired, it returns `PerformerResponse(status="env_blocked", reason=<env reason>)` instead of `changes_requested`.

**Rationale**: A pre-push env failure cannot reuse the `env_cache_health_failed`-on-terminal-success path (line 2112) because there is no success and no PR yet. It also must NOT reuse `changes_requested`, because that status *is* the fix-feedback cycle — the exact misattribution spec-088 fixed for QA. `qa_env_blocked` already encodes the correct semantics ("environment is broken, do not fix code, hold the stage until the cache is repaired"); generalising it to a role-agnostic `env_blocked` lets the implementer reuse the identical routing with no new coordinare branch logic beyond the marker name. Spec-088 deliberately named QA's status `qa_env_blocked` (QA-specific); this spec adds the sibling for code-writing roles rather than overloading QA's.

**Alternatives considered**:
- *Reuse `qa_env_blocked` for the implementer* — rejected: the name and its observability events are QA-scoped; reusing it would make logs lie about which stage blocked.
- *Set `env_cache_health_failed=True` on a `changes_requested` response and special-case it in the changes_requested handler* — rejected: changes_requested returns early down a different path; bolting an env-check onto it duplicates the qa_env_blocked routing we already have and risks the agent still being re-dispatched to "fix" code.

---

## R3 — Where does the self-fix loop live, and how is it bounded?

**Decision**: Two layers.
1. **In-session (primary)** — the implementer role contract/prompt directs the agent to run `test_command` and fix failures within its own turn before declaring done (FR-011). This is where most convergence happens; the agent loops in one process.
2. **Coordinare backstop (bounded)** — when the agent declares done but local tests still fail for a code reason, the performer returns `changes_requested` carrying the failing output AND a marker (`local_test_failed=True`). `monitor_performer.py` increments a per-head `local_fix_counter[head_sha]`; while under `max_fix_attempts` it re-dispatches (existing changes_requested re-dispatch), and when the budget is exhausted it routes the card to **blocked** with the failing output as the reason (FR-008) — never pushing.

The counter is persisted on `PersistedSession` (state_store v6 bump), hydrated/persisted in `daemon.py`, and carried in the live session dict — all parallel to spec-075's `bounce_counter`.

**Rationale**: The performer is a one-shot agentic session; the only "loop" the coordinare controls is re-dispatch via `changes_requested`. Keeping the budget coordinare-side (mirroring `bounce_counter`) reuses proven machinery and survives process restarts via the snapshot. Keeping it **separate** from `bounce_counter` (FR-007, SC-004) is required because cheap local retries must not deplete the expensive remote-bounce budget, and vice versa. Per-head keying matches spec-075: if the agent commits a fix, the head changes and the budget resets for the new code; if it commits nothing, the same head keeps failing and the budget converges to escalation.

**Alternatives considered**:
- *In-process re-prompt loop inside the performer* — rejected: the backend session is one-shot; re-prompting mid-process is not how any existing role works and would diverge from the "Guardrails for Forgetful Models" durable-contract pattern.
- *Reuse `bounce_counter`* — rejected: violates SC-004; conflates local and remote budgets.
- *Unbounded changes_requested* — rejected: thrashes forever on an unfixable failure (spec mandates escalation).

---

## R4 — Config surface and default

**Decision**: Add `LocalTestGateConfig` (pydantic `BaseModel`, `extra="forbid"`) as a sibling of `CIGateConfig` in `config.py`: `enabled: bool = False`, `timeout_seconds: int = Field(default=600, ge=60, le=7200)`, `max_fix_attempts: int = Field(default=2, ge=0, le=20)`. Nested under the same symphony/persona-scope surface that already hosts `CIGateConfig` (spec-075), so the two implementer gates are configured side by side.

**Rationale**: Default `enabled=False` guarantees SC-005 (byte-identical to today when unset). Bounds on `timeout_seconds` mirror `CIGateConfig.pending_timeout_seconds`. `max_fix_attempts` default 2 gives one in-session pass plus a small backstop before escalation; `ge=0` allows "no backstop re-dispatch, escalate immediately on first coordinare-observed failure" as a valid strict mode.

**Alternatives considered**:
- *Global (non-symphony) flag* — rejected: gating is a per-symphony policy choice, consistent with spec-075's `CIGateConfig` placement.
- *Reuse `CIGateConfig`* — rejected: different semantics (local timeout + fix attempts vs remote bounce + pending timeout); overloading one model would force unrelated fields together.

---

## R5 — Timeout classification, no-test-command, and standalone fallback

**Decision**:
- **No `test_command` detected** → helper returns pass-through (`passed=True`, `command=None`), gate is a no-op (FR-001, edge case), exactly as `_run_ci_check` skips when no linter is detected.
- **Timeout** → `run_command` timeout is treated as a genuine failure (US1/US3 path) UNLESS an env-cache signal also fired, in which case env-blocked wins (FR-010). The 600s default is generous so slow suites are not mistaken for hangs.
- **Coordinare package unavailable (standalone performer)** → the `from coordinare.services.ci_detection import detect` import fails with `ImportError`; the helper returns pass-through, identical to `_run_ci_check`'s existing fallback (FR-012). The spec-075 remote gate is the backstop.

**Rationale**: Each behaviour mirrors an already-proven branch in `_run_ci_check` or the spec-088 classification, minimising novel logic and review surface.

**Alternatives considered**: Treating a timeout as env-blocked unconditionally — rejected: a genuinely hung/expensive test is a code/test problem, not an environment one; only an explicit env signal reclassifies it.

---

## R6 — Ordering relative to the lint gate

**Decision**: The local test gate runs **after** `_run_ci_check` passes and **before** push (FR-002). Lint failure short-circuits first (existing behaviour, lines 2806–2815) — lint is cheaper and a lint failure already blocks push.

**Rationale**: Cheapest-first; no point running a multi-minute suite on code that won't lint. Preserves the existing lint behaviour untouched.

---

## Summary of decisions

| # | Question | Decision |
|---|----------|----------|
| R1 | Test execution factoring | New `_run_test_check` helper returning `LocalTestResult`, mirrors `_run_ci_check` |
| R2 | Env-blocked signalling | New role-agnostic `env_blocked` status, routed like `qa_env_blocked` |
| R3 | Self-fix loop + budget | In-session primary + coordinare `changes_requested` backstop bounded by per-head `local_fix_counter`, escalate to blocked |
| R4 | Config | `LocalTestGateConfig` sibling of `CIGateConfig`; disabled by default |
| R5 | Timeout / no-cmd / standalone | Mirror `_run_ci_check` fallbacks; env signal overrides timeout |
| R6 | Ordering | After lint passes, before push |
