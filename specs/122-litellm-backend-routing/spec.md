# Feature Specification: Consolidate All Agent Backends on the LiteLLM Model Gateway

**Feature Branch**: `122-litellm-backend-routing`
**Created**: 2026-06-26
**Status**: Draft
**Input**: User description: "Route ALL agent backends through the LiteLLM-served spark/gpt-oss models, replacing the Ollama-direct routing-table shims, and verify per-backend compatibility."

## Context

The orchestrator dispatches work to several agent backends (claude_code, codex, opencode, junie, pi, openclaw, hermes). Today they reach their self-hosted model through an inconsistent mix: some go through the shared model gateway (LiteLLM), but several were re-pointed **directly at the raw model host** (Ollama) behind per-backend repair shims (wire translation, reasoning-channel stripping, tool-call reassembly). That split happened because, at the time, the gateway mishandled the self-hosted model's output.

That is no longer true. The gateway now serves the target self-hosted models as healthy deployments, on **both** request styles the backends use, and it performs the output repairs server-side that the per-backend shims were doing. The operator wants the **entire fleet consolidated on the single gateway** — one place that serves models, one place that repairs their output — and wants that **compatibility proven, backend by backend, before any further features or bug-fixes proceed.**

This feature does that consolidation on the orchestrator side and proves it. It does **not** change the gateway's own configuration (operator-owned; the target models are already served).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Every backend reaches its model through the single gateway (Priority: P1)

Each agent backend the fleet uses can run its work against the target self-hosted model **through the shared gateway**, instead of connecting directly to the raw model host. The backend that speaks the "thinking-assistant" request style uses the gateway's matching front door; the backends that speak the "chat-completion" request style use the gateway's chat front door. The gateway's master credential is the only upstream secret involved.

**Why this priority**: This is the foundation — until every backend can reach its model through the gateway, the fleet cannot be consolidated and the mixed topology persists.

**Independent Test**: For each backend, point it at the gateway with the target model and run a minimal real task; confirm it reaches the model and returns a response (not a connection/auth/"no such model" error).

**Acceptance Scenarios**:

1. **Given** a backend configured to reach the gateway with the target self-hosted model, **When** it is dispatched for a minimal task, **Then** it connects to the gateway (not the raw model host) and receives a model response.
2. **Given** the backend that speaks the thinking-assistant request style, **When** it is dispatched through the gateway, **Then** it works without an orchestrator-side wire-translation step (the gateway provides that front door).
3. **Given** any backend, **When** it authenticates to the gateway, **Then** it uses only the gateway master credential supplied through the existing redacted secret channel (no secret value appears in any log or record).

---

### User Story 2 - Per-backend compatibility is proven before migration (Priority: P1)

Before any backend is switched over in the live configuration, a **reproducible compatibility check** exercises each backend against the target model through the gateway and records a clear pass/fail with the evidence: did the backend launch, complete a real role task, return non-empty output, and produce output that satisfies its role contract (e.g. a parseable verdict for a verdict-producing role)? The check also determines, per backend, whether any orchestrator-side output-repair step is **still required** or is now **redundant** (because the gateway repairs the output).

**Why this priority**: "Ensure compatibility" is the operator's explicit gate. A backend may only be migrated once it has demonstrably passed this check — guessing risks silently breaking a role in production. This is co-equal P1 with US1.

**Independent Test**: Run the compatibility check across all backends; it emits a matrix (one row per backend) of launched / completed / output-present / contract-satisfied, plus the repair-steps-still-needed determination. Re-running with no changes yields the same matrix.

**Acceptance Scenarios**:

1. **Given** the compatibility check, **When** it runs against a backend through the gateway, **Then** it records whether the backend launched, completed the task, returned non-empty output, and satisfied its role contract.
2. **Given** a backend whose raw output the gateway already repairs, **When** the check runs with the orchestrator's repair step removed, **Then** the backend still passes — marking that repair step redundant for this backend.
3. **Given** a backend that still needs a repair step to pass, **When** the check runs without it, **Then** the backend fails — marking that repair step as one to keep.
4. **Given** the full run, **When** it completes, **Then** a backend is labelled "gateway-compatible" only if it launched, completed, and satisfied its role contract.

---

### User Story 3 - Migrate routing to the gateway and retire redundant repair steps (Priority: P2)

With the compatibility matrix in hand, the orchestrator's routing is re-pointed from the raw model host to the gateway for every backend proven compatible, the now-redundant repair steps are removed, dead/stale model routing entries are cleaned up, and the orchestrator still drives a complete piece of work end-to-end on the gateway-only routing.

**Why this priority**: This is the payoff — a single, simpler routing topology — but it depends on US1 (reachability) and US2 (proof), so it is P2.

**Independent Test**: After migration, inspect the routing configuration — no backend points at the raw model host; only empirically-needed repair steps remain; dead entries are gone. Then drive one unit of work through the full lifecycle and confirm it completes on gateway-only routing.

**Acceptance Scenarios**:

1. **Given** the migrated routing, **When** the orchestrator dispatches any backend, **Then** that backend reaches its model through the gateway, not the raw model host.
2. **Given** a repair step that US2 proved redundant, **When** migration completes, **Then** that step is removed from the routing path.
3. **Given** a repair step that US2 proved still necessary for a backend, **When** migration completes, **Then** that step is retained for that backend.
4. **Given** stale/dead model routing entries (referencing a namespace the gateway no longer serves, or a now-unused direct-host entry), **When** migration completes, **Then** they are removed.
5. **Given** the migrated configuration, **When** a unit of work is driven through the full lifecycle, **Then** it completes without reverting to the raw model host.

---

### Edge Cases

- **A backend fails the compatibility check.** It is NOT migrated; it stays on its current (working) routing, and the matrix records the failure with its evidence. Consolidation proceeds for the backends that passed; the failing one is surfaced for follow-up.
- **A backend needs a repair step the gateway doesn't provide.** That repair step is kept on the gateway path for that backend (US3 scenario 3) — consolidation does not mean blindly stripping every repair step.
- **The gateway is unreachable / returns "no such model" during the check.** The check records this as an environment/availability failure (distinct from a backend incompatibility), so a transient gateway issue is not mistaken for a backend defect.
- **A role intentionally wants a frontier model** (not the self-hosted target). That role is left as-is; this feature consolidates the self-hosted path, it does not force frontier roles onto the self-hosted model.
- **Two request styles, one model.** The thinking-assistant-style backend and the chat-style backends both target the same model through different gateway front doors; both must work.
- **Re-running the check** must be idempotent and side-effect-free on the live configuration (it uses throwaway instances, never the running fleet).

## Requirements *(mandatory)*

### Functional Requirements

**Reachability (US1)**

- **FR-001**: Every agent backend the fleet uses MUST be able to reach the target self-hosted model **through the shared gateway**, configured via that backend's provider base-URL setting, rather than connecting directly to the raw model host.
- **FR-002**: The backend that speaks the thinking-assistant request style MUST reach the gateway through the gateway's matching front door **without** an orchestrator-side wire-translation step.
- **FR-003**: Backend authentication to the gateway MUST use only the gateway master credential delivered through the existing redacted secret channel.

**Compatibility proof (US2)**

- **FR-004**: A reproducible compatibility check MUST exercise each backend against the target model through the gateway and record, per backend: launched, task-completed, output-present, role-contract-satisfied.
- **FR-005**: The check MUST determine, per backend, whether each orchestrator-side output-repair step is still required (kept) or redundant (removed) — by testing the backend with the repair step absent.
- **FR-006**: The check MUST label a backend "gateway-compatible" only when it launched, completed the task, and satisfied its role contract.
- **FR-007**: The check MUST distinguish a gateway availability failure (unreachable / model not served) from a backend incompatibility, so the two are not conflated.
- **FR-008**: The check MUST run against throwaway backend instances and MUST NOT alter the live running fleet or its configuration.

**Migration (US3)**

- **FR-009**: Routing MUST be re-pointed from the raw model host to the gateway for every backend that passed the compatibility check.
- **FR-010**: Output-repair steps proven redundant in FR-005 MUST be removed from the routing path; steps proven still-necessary MUST be retained for the relevant backend.
- **FR-011**: Stale/dead model routing entries (those referencing a namespace the gateway no longer serves, or now-unused direct-host entries) MUST be removed.
- **FR-012**: After migration, the orchestrator MUST drive a complete unit of work through the full lifecycle on gateway-only routing without reverting to the raw model host.
- **FR-013**: A backend that fails the compatibility check MUST NOT be migrated; it MUST remain on its current working routing and be surfaced for follow-up.

**Cross-cutting**

- **FR-014**: No log, record, or notification produced by this work may contain a secret value — only names, counts, and reasons.
- **FR-015**: No new external dependency may be introduced. The gateway's own configuration is out of scope (operator-owned; the target models are already served).
- **FR-016**: The compatibility-check artifact (the matrix and its evidence) MUST be reproducible and recorded so the migration decisions are traceable.

### Key Entities

- **Agent backend**: One of the agent runners the fleet dispatches (the "who runs the CLI"). Each reaches its model through a configurable provider base-URL and speaks one of two request styles (thinking-assistant or chat-completion).
- **Model gateway**: The single shared service that serves the self-hosted models and repairs their output server-side; the consolidation target. Configuration owned by the operator.
- **Target self-hosted model**: The self-hosted model the fleet runs on, served by the gateway in a heavier and a lighter size.
- **Output-repair step**: An orchestrator-side transform that fixed a raw-model output pathology (wire format, reasoning leak, tool-call reassembly). Each may now be redundant because the gateway repairs output.
- **Compatibility matrix**: The per-backend pass/fail record (launched / completed / output-present / contract-satisfied + repair-steps-needed) that gates migration.
- **Routing configuration**: The orchestrator-side mapping of backend → model → endpoint (and any repair steps). The thing being migrated from raw-host to gateway.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of the fleet's agent backends are exercised against the target model through the gateway in the compatibility check (no backend untested).
- **SC-002**: Every backend migrated in the live configuration has a recorded "gateway-compatible" pass in the matrix (no backend migrated on a guess).
- **SC-003**: After migration, 0 backends route to the raw model host (the mixed topology is eliminated for the consolidated set).
- **SC-004**: Every output-repair step remaining in the routing path is justified by a recorded compatibility-check result; every removed step is likewise justified (no unexplained repair steps either way).
- **SC-005**: A complete unit of work is driven through the full lifecycle on gateway-only routing and completes successfully.
- **SC-006**: Zero secret values appear in any log/record/notification produced by this work.
- **SC-007**: The compatibility check is re-runnable and produces a consistent matrix on an unchanged system.

## Assumptions

- The gateway already serves the target self-hosted model in both a heavier and a lighter size as healthy deployments (verified during scoping); this feature does not change the gateway's configuration.
- The gateway repairs the two output pathologies (request-style/wire translation, reasoning-channel separation, structured tool-calls) server-side, so most orchestrator-side repair steps are expected to be redundant — but each is confirmed empirically per backend, not assumed.
- The live routing/config the running fleet uses is operational deployment state (not committed source); the committed surface is the example configs, the compatibility-check harness, and any code that wires or removes repair steps.
- "The fleet's backends" are the agent runners currently configured; a backend no role uses need not be migrated.
- A role that intentionally targets a frontier model is left unchanged.

## Out of Scope

- The gateway's own server configuration / adding models to the gateway (operator-owned; already done for the target model).
- Self-hosted models other than the target model, unless a specific role needs one (decided from the matrix).
- The frontier model catalog, except where a role intentionally uses it.
- The environment-cache / QA-screenshot / database-connectivity work (separate, already addressed).
- Changes to the performer container image.
- Dual-model orchestration beyond pointing single-model routing entries at the gateway.
