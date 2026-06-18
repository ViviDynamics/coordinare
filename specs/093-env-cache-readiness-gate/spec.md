# Feature Specification: Env-Cache Toolchain-Readiness Dispatch Gate

**Feature Branch**: `093-env-cache-readiness-gate`  
**Created**: 2026-06-18  
**Status**: Draft  
**Input**: User description: "Env-cache toolchain-readiness dispatch gate (spec 093) — continues the spec-092 env-cache reliability line. Gate code-running performer dispatch on a real, manifest-driven toolchain-readiness check (re-run each dispatch), kick the cache back to env-bootstrap on failure, and bound the loop with the existing attempt budget."

## User Scenarios & Testing *(mandatory)*

This feature continues the env-cache reliability line opened by spec-091 (stateful service
hosting) and spec-092 (symphony test-env injection). It closes a **dispatch-readiness race**
observed live on the `website` symphony: a QA performer was dispatched against an env-cache
whose ruby toolchain had not finished building. QA was dispatched 14:19–15:40 UTC; the ruby
toolchain was not built until 16:09 UTC and `bootstrap_complete` did not fire until 16:23 UTC.
The result was a "Ruby interpreter not available" QA block that the spec-088 integrity gate
then correctly downgraded to "pass claim with zero execution evidence." The existing
bootstrap-current dispatch guard (`dispatch_performer.env_cache_not_current` /
`bootstrap_in_flight`) held correctly from 16:08 onward, but earlier runs slipped through
because dispatch-readiness keyed on `last_bootstrap_succeeded` **alone** rather than on whether
the toolchain for **this spec sha** is actually present, running, and usable.

### User Story 1 - Dispatch is gated on real toolchain readiness for all code-running stages (Priority: P1)

As the coordinare, before dispatching any performer stage that runs project code (qa,
implementer, reviewer), I confirm the env-cache's declared toolchain is actually present,
running, and usable for the current spec sha — not merely that a prior bootstrap reported
success — so that no performer is ever handed an environment whose interpreter, native
extensions, or backing services are missing.

**Why this priority**: This is the core defect. Keying dispatch readiness on
`last_bootstrap_succeeded` alone allowed code-running performers to start against a
half-built cache, producing hollow "env-blocked" verdicts. Gating on a real readiness
check is the minimum change that closes the race and delivers value on its own.

**Independent Test**: Stand up an env-cache whose readiness check fails (e.g. the toolchain
binary is absent) and request a qa/implementer/reviewer dispatch. Dispatch MUST be withheld
(held, not run) even when `last_bootstrap_succeeded` is True. Then make the readiness check
pass and confirm dispatch proceeds. Stages that do not run project code are unaffected.

**Acceptance Scenarios**:

1. **Given** an env-cache with `last_bootstrap_succeeded=True` but a readiness check that
   fails for the current spec sha, **When** a qa stage dispatch is requested, **Then**
   dispatch is withheld and the reason is recorded as a readiness failure (not dispatched).
2. **Given** the same cache, **When** an implementer or reviewer dispatch is requested,
   **Then** dispatch is likewise withheld — the gate applies to every code-running stage.
3. **Given** an env-cache whose readiness check passes for the current spec sha, **When** any
   code-running stage dispatch is requested, **Then** dispatch proceeds normally.
4. **Given** a stage that does not run project code, **When** dispatch is requested, **Then**
   the readiness gate does not block it.

### User Story 2 - Readiness is a manifest-driven checklist of present/running/working items (Priority: P2)

As the coordinare, I verify readiness with a checklist **derived from the env manifest**, where
each declared item is confirmed not just installed but usable: each declared toolchain's binary
is resolvable via `activate.sh` AND its version matches; each declared native extension is
loadable under the real project dependency manifest; each coordinare-managed service is RUNNING
and healthy (not merely installed). Each item emits a readable OK / FAIL / WARN line, and the
aggregate exit code drives the gate.

**Why this priority**: A readiness gate is only as trustworthy as what it checks. A checklist
derived from the manifest (rather than a hardcoded probe) keeps the gate toolchain-agnostic and
makes "ready" mean every declared dependency is actually working. This builds directly on
US1's gate but is independently valuable: even outside the dispatch path, a manifest-driven
`verify.sh` gives an auditable readiness report.

**Independent Test**: Generate `verify.sh` from a manifest declaring a toolchain, a native
extension, and a coordinare-managed service. Run it against a cache where the service is
installed but NOT running; the service line MUST report FAIL and the aggregate exit code MUST
be nonzero. Start the service and re-run; the line MUST report OK and the exit code MUST be
zero. Confirm a missing `verify.sh` yields the degraded (None) result that does NOT block.

**Acceptance Scenarios**:

1. **Given** a manifest declaring a toolchain, **When** `verify.sh` runs and the binary is
   resolvable via `activate.sh` with a matching version, **Then** the toolchain line reports
   OK; if the binary is unresolvable or the version mismatches, the line reports FAIL and the
   aggregate exit code is nonzero.
2. **Given** a manifest declaring a native extension, **When** `verify.sh` runs and the
   extension is loadable under the real dependency manifest, **Then** the line reports OK;
   if it cannot be loaded, the line reports FAIL.
3. **Given** a manifest declaring a coordinare-managed service (e.g. postgres, redis), **When**
   `verify.sh` runs and the service responds to its health probe, **Then** the line reports
   OK; if the service is installed but not running/healthy, the line reports FAIL.
4. **Given** a test/qa-only nicety (e.g. a browser on PATH, a client utility), **When** it is
   absent, **Then** the line reports WARN and does NOT drive the aggregate exit code to nonzero.
5. **Given** the readiness check is invoked on dispatch, **When** `verify.sh` is absent from the
   cache, **Then** the result is degraded (None) and MUST NOT block dispatch.
6. **Given** any readiness run, **When** lines are emitted to logs and persisted state, **Then**
   they contain only env-var NAMES and file PATHS — never literal secret values.

### User Story 3 - Readiness failure self-heals via bootstrap, bounded by the attempt budget (Priority: P3)

As the coordinare, when the readiness check FAILS for a code-running dispatch, I kick the cache
back to env-bootstrap for the current spec sha and fall into the existing
`env_cache_not_current` / `bootstrap_in_flight` hold until bootstrap completes and readiness
passes — and I bound this dispatch→bootstrap→dispatch loop with the EXISTING
`env_bootstrap_max_attempts` budget so a genuinely-broken environment surfaces as an actionable
env-blocked verdict instead of thrashing forever.

**Why this priority**: This makes the gate self-healing rather than merely obstructive, and the
attempt-budget bound prevents an unfixable environment from looping indefinitely. It depends on
US1's gate and US2's checklist being in place, so it is sequenced last.

**Independent Test**: Force readiness to fail on dispatch and confirm a re-bootstrap is
triggered for the current spec sha and dispatch is held under `bootstrap_in_flight`. Make
bootstrap converge so readiness passes, and confirm dispatch then proceeds. Separately, force
readiness to fail persistently and confirm that after `env_bootstrap_max_attempts` the loop
stops and an actionable env-blocked verdict is produced instead of an infinite loop.

**Acceptance Scenarios**:

1. **Given** a readiness FAIL on a code-running dispatch and attempts remaining under the
   budget, **When** the gate fires, **Then** a re-bootstrap is triggered for the current spec
   sha and dispatch is held under the existing `env_cache_not_current` / `bootstrap_in_flight`
   path until bootstrap completes.
2. **Given** a held dispatch whose re-bootstrap converges so readiness passes, **When** the
   next dispatch is attempted, **Then** it proceeds normally.
3. **Given** readiness that keeps failing, **When** the `env_bootstrap_max_attempts` budget is
   exhausted, **Then** the loop stops and an actionable env-blocked verdict is produced (no
   thrashing).

### Edge Cases

- **`verify.sh` absent** (older cache, never regenerated): readiness is degraded (None) and MUST
  NOT block dispatch — a missing checklist is not a failure signal (pins the existing
  `_verify_env_cache_clean` contract).
- **Readiness passes but bootstrap is concurrently in flight** for the current spec sha: the
  existing `bootstrap_in_flight` hold still governs; readiness passing does not override an
  in-flight bootstrap.
- **Version mismatch** (toolchain present but wrong version for the spec sha): treated as FAIL,
  not OK — the cache is stale for this sha and must be re-bootstrapped.
- **Coordinare-managed service installed but not running**: FAIL — "installed" is not "ready."
- **Non-blocking nicety absent** (browser on PATH, client utility): WARN only — never blocks
  dispatch (the browser-on-PATH item is a known harmless WARN per the website findings).
- **Readiness check times out or errors in clean-context exec**: must resolve to a definite
  block-or-degrade decision, never silently pass.
- **Genuinely-broken environment**: bounded by `env_bootstrap_max_attempts`; surfaces as an
  actionable env-blocked verdict rather than an infinite dispatch→bootstrap loop.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The coordinare MUST gate dispatch for ALL code-running performer stages (qa,
  implementer, reviewer) on a real toolchain-readiness check for the current spec sha, NOT on
  `last_bootstrap_succeeded` alone. Stages that do not run project code MUST NOT be gated.
- **FR-002**: Readiness MUST be verified by a manifest-driven checklist (coordinare-owned
  `verify.sh` derived from the env manifest). For each declared toolchain, the check MUST
  confirm the binary is resolvable via `activate.sh` AND its version matches (mismatch ⇒ FAIL).
  For each declared native extension, the check MUST confirm it is loadable under the real
  project dependency manifest (not loadable ⇒ FAIL). For each coordinare-managed service, the
  check MUST confirm it is RUNNING and healthy via its health probe (installed-but-not-running
  ⇒ FAIL).
- **FR-003**: Test/qa-only niceties (e.g. a browser on PATH, a client utility) MUST be reported
  as non-blocking WARN and MUST NOT drive the aggregate exit code to nonzero.
- **FR-004**: Each checklist item MUST emit a human-readable OK / FAIL / WARN line, and the
  aggregate exit code MUST drive the gate decision.
- **FR-005**: The readiness check MUST reuse the existing `_verify_env_cache_clean` seam:
  True on exit 0, False on nonzero, None when `verify.sh` is absent. A None (degraded) result
  MUST NOT block dispatch.
- **FR-006**: The readiness check MUST be re-run on EACH dispatch (one clean-context container
  exec) — a stale cached pass MUST NOT be trusted.
- **FR-007**: On readiness FAILURE for a code-running dispatch, the coordinare MUST trigger a
  re-bootstrap for the current spec sha and fall into the existing `env_cache_not_current` /
  `bootstrap_in_flight` hold until bootstrap completes and readiness passes, then proceed.
- **FR-008**: The dispatch→bootstrap→dispatch loop MUST be bounded by the EXISTING
  `env_bootstrap_max_attempts` budget. When exhausted, the coordinare MUST surface an actionable
  env-blocked verdict instead of looping indefinitely.
- **FR-009**: The manifest, logs, and persisted state MUST carry only env-var NAMES and file
  PATHS — never literal secret values. Readiness/verification decisions MUST be emitted as
  observability using keys/paths only (carried from spec-091/092, non-negotiable).
- **FR-010**: Coordinare and performer code MUST remain toolchain-agnostic — no hardcoded
  knowledge of specific version managers (rbenv/nvm/asdf) in coordinare logic. The checklist
  MUST be derived from the manifest, and all toolchain-specific probing MUST live in the
  generated `activate.sh` / `verify.sh`, not in coordinare Python.

### Key Entities *(include if feature involves data)*

- **Env manifest**: The existing per-symphony declaration of toolchains, native extensions, and
  coordinare-managed services from which the readiness checklist is derived. Carries names/paths
  only, never secret values.
- **Readiness checklist (`verify.sh`)**: Coordinare-owned shell script generated from the
  manifest. Emits one OK/FAIL/WARN line per declared item; aggregate exit code is the gate
  signal. Toolchain-specific probing lives here, not in coordinare Python.
- **Readiness result**: The tri-state outcome of a readiness run — pass (exit 0), fail (nonzero),
  or degraded/None (`verify.sh` absent). Degraded MUST NOT block.
- **Attempt budget (`env_bootstrap_max_attempts`)**: Existing per-cache bound on the
  dispatch→bootstrap→dispatch loop; exhaustion converts thrashing into an actionable
  env-blocked verdict.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: No code-running performer (qa, implementer, reviewer) is dispatched against an
  env-cache whose toolchain readiness has not passed for the current spec sha — including caches
  where `last_bootstrap_succeeded` is True but the toolchain is not yet present/usable.
- **SC-002**: Zero hollow "env-blocked" QA verdicts attributable to dispatching before toolchain
  readiness (the live website failure mode where QA ran before ruby was built does not recur).
- **SC-003**: A readiness checklist run reports, per declared manifest item, whether it is
  present, running, and working — with each item visibly OK, FAIL, or WARN — and the gate
  decision matches the aggregate (any blocking FAIL ⇒ withheld; only WARN/OK ⇒ proceeds).
- **SC-004**: When readiness fails, the cache is re-bootstrapped for the current spec sha and
  dispatch resumes once readiness passes — bounded so that a persistently-failing environment
  surfaces an actionable env-blocked verdict within `env_bootstrap_max_attempts` rather than
  looping indefinitely.
- **SC-005**: Readiness is re-evaluated on every dispatch; a cache that passed previously but
  regressed is caught on the next dispatch rather than trusted from a stale result.
- **SC-006**: No logged or persisted readiness record contains a literal secret value — only
  env-var names and file paths appear.

## Out of Scope *(YAGNI)*

- Caching readiness results across dispatches — the check is deliberately re-run each dispatch
  (FR-006).
- Parallel / multi-host env-caches.
- New external dependencies.
- The separate responsive-CSS CI-convergence follow-up on the website symphony (agent
  work-quality / model-capability territory; tracked independently).

## Assumptions & Dependencies

- **This gate assumes a COMPLETE env manifest.** A manifest-driven checklist can only verify
  what the manifest declares. If a required dependency is never declared (e.g. an empty
  `services: []` from a flaky inference run, so postgres is absent from the manifest entirely),
  `verify.sh` has nothing to check for it and the readiness gate can return a false-OK for that
  undeclared item. This is a known, deliberate boundary, not an oversight.
- **Manifest completeness vs. manifest realization are separate concerns.** Spec-093 hardens
  manifest *realization* — given a declared item, is it actually present, running, and usable
  before we dispatch? Manifest *completeness* — is the right item even declared? — is owned by
  the manifest-derivation path: the authoritative `.coordinare/score.json` manual override (when
  present, trusted over inference) else the LLM service-inference agent reading the repo's
  `cache_inputs`. Closing inference-reliability gaps (so e.g. postgres is reliably declared) is
  explicitly OUT of scope for 093 and tracked on the inference/score.json line.
- **The spec-088 QA-verdict-integrity gate remains the false-OK backstop.** Where an incomplete
  manifest lets a hollow environment through this readiness gate, the spec-088 integrity gate
  (`monitor_performer.qa_env_blocked`) still rejects a QA "pass" backed by zero execution
  evidence. 093 and 088 are defense-in-depth: 093 prevents dispatch against a known-unready
  cache; 088 catches passes that lack real execution regardless of cause.
