# Feature Specification: QA Verdict Integrity & Performer Environment Reliability

**Feature Branch**: `088-qa-verdict-integrity`
**Created**: 2026-06-11
**Status**: Draft
**Input**: User description: "QA verdict integrity and performer environment reliability: close the env_limited false-pass loophole, require execution evidence behind any claimed pass, gate terminal success on env-cache health, unify backend PATH/env-merge policy, add bootstrap retry circuit breaker, persist bootstrap success across restarts, and surface secret-refresh/services-start failures."

## Problem Statement

A live QA run posted "**Result: PASSED** — Criteria checked: 4, passed: 4" on a pull request while *simultaneously* reporting "Environment Blocker: Ruby runtime is missing… No visual artifacts captured." The card advanced as a clean pass even though nothing was executed, nothing was screenshotted, and the environment was known-broken. Separately, environment failures (toolchain not reaching an agent's shell, a failing bootstrap retried forever, secrets going stale mid-session) stall or silently degrade performer runs.

Code audit traced both clusters to specific defects (file:line refs as of current main):

- **A1 — env-limited false-pass loophole**: the unsubstantiated-pass guard exempts environment-limited runs entirely, so a run claiming `criteria_passed=4` with zero executed checks, zero new tests, and zero visual evidence still emits a clean pass (`agent/performer/src/performer/main.py:840`, created by the interaction of commits `2b253aa` and `d22d84f`).
- **A2 — criteria counts taken on faith**: `criteria_checked`/`criteria_passed` are accepted without any cross-check against execution evidence (`main.py:2279-2296`).
- **A3 — terminal verdict ignores env health**: coordinare advances a card on `qa_passed` without consulting the `env_cache_health_failed` flag it detected earlier in the same poll (`src/coordinare/graph/nodes/monitor_performer.py:2031-2057`; flag handled at ~1729 but never re-checked).
- **A4 — no app-boot evidence gate**: nothing requires evidence the application actually booted (or the browser was reachable) before UI/visual criteria count as "checked".
- **A5 — broken evidence links rendered**: failed screenshot uploads still render as links in the PR comment (`main.py:2379-2394`).
- **B1 — backend env-policy asymmetry**: hermes passes the full env-cache PATH into its subprocess (`hermes.py:376`); junie does the same (`junie.py:311`) and, being a Node CLI, is exposed to the same startup-crash class fixed for claude_code in PR #110. Six backends implement four different PATH policies.
- **B2 — bootstrap retries forever**: a failing env bootstrap is re-dispatched every cooldown (~100s observed live) with no retry cap, no escalating backoff, no terminal state (`src/coordinare/services/env_cache.py:386-403`).
- **B3 — restart re-bootstrap tax**: bootstrap success is not persisted, so every coordinare restart triggers a full (~13 min) re-bootstrap even when the on-disk cache is complete and verifiable.
- **B4 — secret refresh failures swallowed**: a failed mid-session credential refresh is logged and ignored; the performer continues with a stale token and fails later with auth errors (`src/coordinare/services/http_performer_service.py:418-431`; observed live).
- **B5 — observability gaps**: a services-start timeout is a warning that vanishes (root cause invisible when QA later can't reach its database), and malformed performer result payloads become a generic "error" with no diagnostic.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A QA pass always rests on evidence (Priority: P1)

As the operator, when a QA performer cannot actually execute checks (missing runtime, app won't boot, browser unavailable), the QA verdict must say so honestly — it must never surface as an unflagged clean PASS. When QA *does* pass, every claimed criterion is backed by observable evidence (an executed command with exit code, a committed test, or a captured artifact).

**Why this priority**: The QA verdict is the load-bearing trust signal for every downstream stage (review, merge, close). A false PASS poisons the whole lifecycle and erodes the operator's trust in unattended runs — it is the single defect that most undermines "coordinare successfully completing all performers."

**Independent Test**: Feed the QA result-handling layer a synthetic result claiming `criteria_passed > 0` with empty evidence arrays (with and without an environment error). Verify the verdict is downgraded to "unverified / environment-blocked", the PR comment says so, and the card does not advance as a clean pass.

**Acceptance Scenarios**:

1. **Given** a QA result with `environment_error` set, zero executed checks, zero new tests, and zero visual evidence, **When** the run claims `criteria_passed=4`, **Then** the outcome is classified as *environment-blocked / unverified* (not `qa_passed`), the PR comment leads with the blocker, and the coordinare does not advance the card as a clean pass.
2. **Given** a QA result with no environment error that claims `criteria_passed > 0` with zero evidence, **Then** the result is treated as unsubstantiated (existing behavior preserved) — the env-limited exemption no longer bypasses this.
3. **Given** a QA result whose card has UI/visual acceptance criteria, **When** no evidence shows the application booted (e.g., a health-check command among executed checks), **Then** those visual criteria are counted as *unverified*, not passed.
4. **Given** a QA result that legitimately executed checks (non-empty executed checks with commands and exit codes), **When** all criteria pass, **Then** the verdict is `qa_passed` exactly as today — no regression for honest passes.
5. **Given** a QA run that captured screenshots but whose upload failed, **Then** the PR comment lists the capture attempt under "capture blockers" instead of rendering a dead link.

---

### User Story 2 - Terminal success respects environment health (Priority: P1)

As the operator, when a performer reports terminal success but the same poll shows the environment health check failed, the card must not advance silently — the discrepancy must be surfaced and resolved (re-verify or re-run) before the lifecycle continues.

**Why this priority**: This is the coordinare-side half of the false-pass hole; fixing the performer side alone still lets a stale/broken environment leak a tainted success through.

**Independent Test**: Simulate a monitor poll whose status payload contains both a terminal-success marker and `env_cache_health_failed`. Verify the card is routed to a blocked/re-verify path with a structured reason rather than advancing.

**Acceptance Scenarios**:

1. **Given** a performer status with a terminal success marker AND `env_cache_health_failed` present, **When** the monitor evaluates the verdict, **Then** the stage does not advance; the card is held with a structured reason and the environment is re-verified before any retry.
2. **Given** a terminal success without the health flag, **Then** the stage advances exactly as today.

---

### User Story 3 - Every backend gets the same environment contract (Priority: P2)

As the operator, an agent's shell must see the project toolchain (e.g., the pinned Ruby) regardless of which backend runs the stage, and no backend's own CLI may crash because the project pins an older runtime. One shared policy, used by all six backends, with per-backend tests.

**Why this priority**: The PATH-policy divergence already produced the "QA has no Ruby" failure on one backend while five others worked; junie is exposed to the same crash class today. Unifying removes an entire category of "works on backend X, dies on backend Y" incidents.

**Independent Test**: A single shared test exercises the env-merge helper; per-backend tests assert each backend launches its subprocess with image directories first and cache toolchain directories appended, with all non-PATH cache variables preserved.

**Acceptance Scenarios**:

1. **Given** an env cache whose PATH pins an old Node, **When** any backend launches its agent CLI, **Then** the CLI resolves the image's runtime first (no startup crash) while the cache-only tools (ruby, bundle) remain resolvable by the agent's shell.
2. **Given** the shared helper changes, **Then** all six backends inherit the change without per-backend edits (one definition site).

---

### User Story 4 - A failing bootstrap stops burning slots (Priority: P2)

As the operator, when an environment bootstrap fails repeatedly for the same spec content, the system must stop re-dispatching it after a bounded number of attempts, escalate the failure to me with the verification output, and stop consuming performer capacity until the spec changes or I intervene.

**Why this priority**: Observed live: identical bootstrap failures re-dispatched every ~100 seconds indefinitely — wasted slots, log noise, and blocked consumer cards with no operator signal.

**Independent Test**: Drive the bootstrap path with a deterministic failure; verify attempt counting, escalating cooldowns, a terminal exhausted state after N attempts, a notification, and automatic reset when the spec content (SHA) changes.

**Acceptance Scenarios**:

1. **Given** a bootstrap that fails N consecutive times for the same spec SHA, **Then** no further bootstrap is dispatched for that SHA, a `bootstrap_exhausted` event/notification fires with the last verification output, and consumer cards are held with that reason (not a generic "waiting").
2. **Given** an exhausted state, **When** the spec SHA changes (or the operator clears the state), **Then** bootstrapping resumes with a fresh attempt budget.
3. **Given** intermittent failures, **Then** cooldowns escalate between attempts (no fixed ~100s hammering).

---

### User Story 5 - Restarting coordinare doesn't repeat finished work (Priority: P3)

As the operator, restarting coordinare with an intact, verified environment cache must not trigger a full re-bootstrap; a cheap on-disk re-verification must suffice to resume dispatching consumer cards within a minute or two.

**Why this priority**: Every restart currently costs ~13 minutes of bootstrap before any card moves — a real tax during live debugging and deploys, but it only wastes time (it does not corrupt anything), hence P3.

**Independent Test**: Persist a successful bootstrap, restart the daemon, and verify consumers dispatch after only the clean-room verification runs (no bootstrap dispatch) when verification passes; and that a failing verification still triggers a full re-bootstrap.

**Acceptance Scenarios**:

1. **Given** a prior successful bootstrap for spec SHA S and an intact cache on disk, **When** coordinare restarts, **Then** the clean-room verifier runs; if it passes, consumer dispatch resumes without any bootstrap dispatch.
2. **Given** the on-disk cache was wiped or verification fails, **Then** a full bootstrap is dispatched exactly as today (the phantom-success protection is preserved).

---

### User Story 6 - Failures are visible where they happen (Priority: P3)

As the operator, when a mid-session credential refresh fails, when project services fail to start, or when a performer returns a malformed result, I must see a structured, attributable event at the moment it happens — not a downstream symptom hours later.

**Why this priority**: These are observability gaps, not correctness bugs by themselves — but they directly caused hours of misdiagnosis this week (a stale-token 401 surfaced as a generic session end; a services-start timeout surfaced as "QA can't reach redis").

**Independent Test**: Unit-drive each failure path and assert a structured event with actionable fields (what failed, why, what the system will do next) is emitted, and — for the credential case — that the session is marked degraded after a bounded retry.

**Acceptance Scenarios**:

1. **Given** a credential refresh that fails mid-session, **Then** the refresh is retried once; on second failure the session is marked degraded/blocked with a structured reason (never silently continued with a stale token).
2. **Given** a services-start script that times out or exits non-zero, **Then** a structured error event is emitted naming the script, the timeout/exit code, and output tail — and the QA evidence layer can reference it as an environment blocker.
3. **Given** a performer result whose payload fails to parse, **Then** the parse error and a truncated payload sample are logged with the performer id (never a bare generic "error").

---

### Edge Cases

- A QA run with a *legitimate* environment blocker that still executed *some* checks (e.g., linters ran but the app couldn't boot): partial evidence must be credited for the criteria it covers; only un-evidenced criteria are downgraded to unverified.
- A card with no UI/visual acceptance criteria must not require app-boot evidence (the gate is conditional on criteria type).
- A QA model that fabricates `executed_checks` entries (plausible commands/exit codes that never ran) is out of scope for hard prevention — but the evidence contract must require commands + exit codes so fabrication is auditable in the PR comment.
- Bootstrap exhaustion must not wedge the symphony forever: SHA change, operator clear, or daemon-config change must all reset the budget.
- Persisted bootstrap success must be keyed by spec SHA — a stale success for an old SHA must never satisfy a new SHA.
- Backends whose agents *do* re-source the activation script per command (openclaw/pi/codex) must not regress when moved to the append policy (append is a superset of strip for them).
- An empty image PATH (rare container misconfiguration) must fall back to a sane system default rather than launching the CLI with only cache directories.

## Requirements *(mandatory)*

### Functional Requirements

**Cluster A — verdict integrity**

- **FR-001**: A QA result claiming one or more passed criteria MUST be backed by execution evidence (at least one executed check with command and exit code, a committed test, or a validated visual artifact). The environment-limited exemption MUST NOT bypass this rule.
- **FR-002**: A QA run that reports an environment blocker and provides zero execution evidence MUST be classified as *environment-blocked / unverified* — a distinct outcome that is neither a clean pass nor a code-defect failure — and the PR comment MUST lead with the blocker.
- **FR-003**: An environment-blocked/unverified outcome MUST NOT advance the card as a clean pass. It MUST surface to the coordinare with a structured reason so the environment can be repaired and the QA stage re-run.
- **FR-004**: The coordinare's terminal-verdict evaluation MUST consult environment-health signals (`env_cache_health_failed` or equivalent) present in the same status payload; a terminal success accompanied by a failed environment-health signal MUST NOT advance silently — the card is held and the environment re-verified before the stage can complete.
- **FR-005**: When a card's acceptance criteria include UI/visual elements, QA evidence MUST include proof the application booted (e.g., a health-check command among executed checks). Visual criteria lacking boot evidence count as unverified, not passed.
- **FR-006**: Visual-evidence entries MUST be validated (upload succeeded / path exists) before being rendered as links in the PR comment; failed captures MUST be listed as capture blockers instead.
- **FR-007**: Criteria accounting MUST be cross-validated: `criteria_passed` greater than the number of evidence-backed criteria MUST downgrade the surplus to unverified and mark the result accordingly.

**Cluster B — environment reliability**

- **FR-008**: All performer backends MUST obtain their subprocess environment from a single shared merge policy: image PATH directories first, env-cache toolchain directories appended (deduplicated), all non-PATH cache variables preserved, with existing precedence (process env < cache < git < tool) unchanged. The policy MUST have shared tests plus per-backend conformance tests.
- **FR-009**: Bootstrap retries MUST be bounded per spec SHA with escalating cooldowns. After the attempt budget is exhausted, the system MUST enter a terminal exhausted state for that SHA: no further dispatches, a notification carrying the last verification output, and consumer holds that name the exhausted state as the reason.
- **FR-010**: The exhausted state MUST reset automatically when the spec SHA changes and MUST be clearable by the operator without restarting the daemon.
- **FR-011**: Bootstrap success MUST be persisted (keyed by spec SHA). On restart, when the persisted SHA matches the current SHA and the on-disk cache passes the clean-room verification, consumer dispatch MUST resume without a bootstrap dispatch. A failed verification or SHA mismatch MUST trigger a full bootstrap (preserving the existing phantom-success protection).
- **FR-012**: A failed mid-session credential refresh MUST be retried once; a second failure MUST mark the session degraded/blocked with a structured reason and MUST prevent the system from treating subsequent auth failures of that session as unexplained.
- **FR-013**: Services-start failures (timeout or non-zero exit) MUST emit a structured error event (script, cause, output tail) that downstream QA evidence can cite as an environment blocker.
- **FR-014**: Malformed performer result payloads MUST be logged with the parse error, performer id, and a truncated payload sample before being mapped to an error status.

### Key Entities

- **QA Evidence Record**: the structured result a QA performer returns — criteria counts, executed checks (command + exit code), new tests, visual artifacts, capture blockers, environment error. The contract this feature hardens.
- **QA Verdict**: the classified outcome of a QA stage — clean pass, code-defect failure, or (new) environment-blocked/unverified. Determines card advancement.
- **Bootstrap Attempt Budget**: per-spec-SHA counter with escalating cooldowns and a terminal exhausted state; persisted with the env-cache state.
- **Persisted Bootstrap Success**: durable record (spec SHA → succeeded) that survives daemon restarts and is validated by the clean-room verifier before reuse.
- **Backend Environment Policy**: the single shared definition of how a performer backend composes its subprocess environment from image, cache, git, and tool sources.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Zero unflagged clean passes from QA runs with no execution evidence: replaying the PR #159 scenario (environment blocker + zero evidence + claimed 4/4) yields an environment-blocked/unverified outcome, never `qa_passed`.
- **SC-002**: 100% of backends (all six) compose their subprocess environment via the shared policy, each covered by a conformance test; zero per-backend PATH-policy code remains.
- **SC-003**: A deterministically failing bootstrap stops dispatching within the attempt budget (default ≤ 3 per SHA) and produces exactly one operator notification; observed slot consumption from a failing bootstrap drops from unbounded (~every 100s) to the bounded budget.
- **SC-004**: Coordinare restart with an intact verified cache resumes consumer dispatch in under 2 minutes (versus ~13 minutes today), while a corrupted cache still triggers a full re-bootstrap.
- **SC-005**: Every secret-refresh failure, services-start failure, and malformed-result event appears as a structured log event with actionable fields; zero such failures are observable only via downstream symptoms in a full QA-cycle run.
- **SC-006**: All existing honest-pass and honest-fail QA flows are unchanged (regression suite green); the new unverified outcome appears only when evidence is absent.

## Assumptions

- The QA evidence JSON contract may be extended (new outcome classification, boot-evidence expectations) as long as existing fields keep their meaning; performer prompt text will be updated in the same change.
- "Environment-blocked / unverified" surfaces to the coordinare as a distinct non-success status; the default policy is to hold the card for environment repair + re-run (not to fail the card as a code defect, and not to advance it).
- The clean-room verifier (`verify.sh` run in a fresh context) is the accepted arbiter of cache validity for restart-resume (it already exists and is coordinare-owned).
- The bootstrap attempt budget default (3) and cooldown escalation (e.g., 2× per attempt) are configurable; exact values are an implementation detail.
- Fabricated evidence (a model inventing commands it never ran) is mitigated by auditability (commands + exit codes in the PR comment), not prevented — full execution attestation is out of scope.

## Out of Scope

- Model capability work (choosing/benchmarking models for roles).
- New performer backends or changes to backend selection.
- Dashboard/UI changes beyond surfacing the new structured events in existing log streams.
- CI workflow changes (the version-sync bot trap and the playwright download flake were fixed separately).
- Hard attestation/sandboxed re-execution of QA-claimed commands.
