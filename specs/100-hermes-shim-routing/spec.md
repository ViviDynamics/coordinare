# Feature Specification: Route the hermes (tech_writer) Backend Through the Self-Hosted Normalizer Shim

**Feature Branch**: `100-hermes-shim-routing`  
**Created**: 2026-06-21  
**Status**: Draft  
**Input**: User description: "Route the hermes (tech_writer / documenting) backend through the self-hosted normalizer shim so its contract-bound JSON output is normalized before its strict parser."

## Overview

The `documenting` stage runs the hermes harness (role = tech_writer) against a self-hosted model. tech_writer is a **JSON-only** role: hermes requires the model to return a clean JSON object (the documentation-file payload it commits); if it can't parse that, it terminal-errors (`malformed_output`) and the card is **blocked at documenting — the last stage before review**.

Every other contract-bound role (assessor, reviewer, QA, security) gets the model's raw output **normalized first** — the self-hosted robustness layer strips reasoning leakage, promotes a reasoning-only answer into content, and completes the response envelope before the strict harness parser sees it. **hermes is the one contract-bound backend excluded from that layer** — it talks its provider directly. So when its model is a reasoning model (which wraps the JSON in reasoning preamble / markdown fences), the unnormalized output fails hermes's strict parse and the card blocks.

This was reproduced live (2026-06-21): cards blocked at documenting on `malformed_output`. It is **not a model-capability gap** — the same model that succeeds for the *normalized* assessor/reviewer roles fails here, purely because hermes's path has no normalization.

This feature makes the hermes backend **routable through the same normalizer shim** the other backends use — opt-in via the operator routing table, with the completion-style health probe (since tech_writer is non-tool-calling) — so its model's output is normalized before hermes parses it. When no routing entry names hermes, behavior is unchanged.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The documenting stage's output is normalized before hermes parses it (Priority: P1)

When the hermes/tech_writer model returns output wrapped in reasoning text or markdown (as reasoning models do), the self-hosted layer normalizes it to clean parseable content before hermes's strict JSON parser sees it — so the documenting stage produces its doc commit and the card advances to review instead of blocking on `malformed_output`.

**Why this priority**: This is the live blocker — cards clear every other stage and then fail at documenting purely because hermes lacks the normalization the other roles have. It is the MVP.

**Independent Test**: Route hermes at a source whose output carries a reasoning preamble / fenced JSON; verify the harness receives clean parseable content and the documenting stage succeeds (no `malformed_output`).

**Acceptance Scenarios**:

1. **Given** a hermes routing entry through the normalize shim, **When** the upstream returns reasoning-wrapped or empty-but-reasoned output, **Then** the normalized result hermes receives is clean, parseable JSON and the stage completes.
2. **Given** the same routing, **When** the upstream returns a clean response already, **Then** it passes through unchanged.

---

### User Story 2 - hermes is admitted through a completion health probe, not fail-closed (Priority: P1)

Because tech_writer is a non-tool-calling JSON-completion role, hermes's routed target is health-gated with the **completion** probe (it judges a non-empty normalized completion as healthy), not the tool-call probe — so a healthy model is admitted instead of being wrongly rejected for "no tool call." A persistently broken upstream is still gated (fail-closed / auto-reroute).

**Why this priority**: Without this, routing hermes through the shim would fail-close every documenting job (the tool-call probe rejects a non-tool-calling model) — strictly worse than today. Same priority as US1.

**Independent Test**: Route hermes through a completion-mode target; verify a normal completion → admitted, an empty/broken upstream → gated.

**Acceptance Scenarios**:

1. **Given** a hermes routing target with the completion probe, **When** the upstream returns a normal completion, **Then** the target is healthy and the job proceeds.
2. **Given** the same, **When** the upstream is empty/non-success across the probe, **Then** the target is gated (fail-closed, or auto-rerouted if a fallback is declared) — never admitted onto a broken path.

---

### User Story 3 - hermes addresses the correct wire path through the shim (Priority: P2)

The hermes harness appends its own request path to its provider base URL, so the loopback address handed to it must carry the served wire-path prefix — such that hermes's appended path lands on a route the shim actually serves. A misaddressed base is caught at the startup health probe, never a silent mid-job failure.

**Why this priority**: A path mismatch would make hermes's real requests miss the shim's served routes and fail after the card is already in flight. Correct addressing is required for US1/US2 to actually work end-to-end, but it rides on them.

**Independent Test**: Launch the hermes shim; verify the provider base URL handed to hermes resolves (with hermes's appended path) to a route the shim serves, and the startup probe exercises that same path.

**Acceptance Scenarios**:

1. **Given** hermes routed through the shim, **When** the loopback base is set, **Then** hermes's appended request path resolves to a served shim route (no 404).
2. **Given** a misconfigured base, **When** the startup probe runs, **Then** it surfaces unhealthy (fail-closed) rather than letting the card 404 mid-job.

---

### Edge Cases

- **No routing entry for hermes**: byte-for-byte unchanged — hermes talks its configured provider directly, as today (opt-in).
- **Clean upstream output**: normalization is a pass-through; stage unchanged.
- **Reasoning-only / fenced output**: normalized to clean content before parse (the fix).
- **Persistently empty/broken upstream**: completion probe gates unhealthy → fail-closed or auto-reroute; never admitted.
- **Other backends**: unaffected — only the hermes path changes, and only when a hermes routing entry exists.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The hermes backend MUST be eligible to route through the existing self-hosted normalize-mode shim when a routing entry resolves its (backend, model) pair — i.e. hermes is no longer excluded from the layer and has a provider-base-URL override the launch seam can repoint.
- **FR-002**: When routed, the hermes model's response MUST pass through the target's declared normalizer chain (reasoning-strip / reasoning-content promotion, envelope completion as applicable) BEFORE hermes's strict parser consumes it.
- **FR-003**: The hermes routed target MUST be health-gated with the **completion** probe mode (non-tool-calling JSON role), and remain timeout-bounded and fail-closed (or auto-reroute when a fallback is declared) on an empty/broken upstream.
- **FR-004**: The loopback base URL handed to hermes MUST carry the wire-path prefix hermes's harness expects (it appends its own path), so the appended path resolves to a route the shim serves; a misaddressed base MUST be caught at the startup probe, never a silent mid-job 404.
- **FR-005**: Default-safe / opt-in: with NO routing entry naming hermes, hermes's behavior MUST be byte-for-byte unchanged, and NO other backend's path may change. Activation is purely adding a hermes routing entry to the operator routing table (and mounting that table into the hermes performer).
- **FR-006**: Health/normalizer decisions MUST be observable and secret-free — only names / shapes / status / resolved-action / probe-mode, never tokens or raw model output (carried invariant). Health gating stays the only fail-closed surface; normalizers stay fail-open.
- **FR-007**: No new external dependency; the single-host single-process snapshot state model is unchanged; the routing table remains a config surface.

### Key Entities *(include if feature involves data)*

- **hermes routing eligibility**: hermes's membership in the set of backends the self-hosted layer can route + its provider-base-URL env mapping — the gate that lets the launch seam repoint it at the shim.
- **hermes loopback base address**: the shim's loopback URL plus the wire-path prefix hermes's harness appends to, so its real request lands on a served route.
- **Health decision record (reused)**: the existing secret-free probe-outcome record, carrying probe mode (completion) for the hermes target.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With hermes routed through the normalize shim, a reasoning-wrapped / fenced model output that previously caused `malformed_output` at documenting is normalized to clean parseable content and the stage completes — 0 `malformed_output` blocks attributable to un-normalized reasoning output.
- **SC-002**: A hermes completion-mode target is admitted (healthy) on a normal completion and gated (fail-closed / rerouted) on an empty/broken upstream — 0 false-negative "no tool call" rejections, 0 fail-open admissions onto a broken path.
- **SC-003**: hermes's real requests through the shim hit a served route (0 mid-job 404s); a misaddressed base is caught at startup.
- **SC-004**: With no hermes routing entry, 100% of hermes jobs and all other backends behave identically to before this feature (no regression).
- **SC-005**: 100% of the new health/normalizer decision records carry only non-secret fields (mode / status / action / names) — 0% contain tokens or raw model output.

## Assumptions

- The self-hosted layer already provides normalize-mode shimming, a reasoning-strip/promote normalizer, envelope completion, and (from 099) a completion health-probe mode — this feature extends eligibility to hermes, it does not build new normalization.
- hermes reads its provider base URL from its existing env var; the launch seam can repoint that env at the shim loopback the same way it does for other backends.
- The hermes harness appends a fixed wire path (e.g. `/chat/completions`) to its provider base; the shim serves that path under a known prefix (e.g. `/v1`).
- The routing table is the operator config surface; adding a hermes entry follows the existing target-descriptor pattern (strategy + normalizers + probe mode).
- Model choice for hermes is operator-owned (the routing entry's model); this feature does not pick or change the model.

## Dependencies

- Spec 078 (self-hosted robustness layer: normalize-mode shim, health gate, routing table) — the mechanism this extends to hermes.
- Spec 098 + #130 (reasoning-content promotion / reasoning strip; envelope completion 082) — the normalizers applied on the hermes path.
- Spec 099 (completion health-probe mode) — the probe mode the hermes target uses.
- The operator routing table (`SELFHOSTED_ROUTING_CONFIG` YAML) + the hermes performer mount — where activation happens (config/ops).

## Out of Scope

- Changing hermes's tech_writer JSON contract or the JSON-only role set.
- The documenting workflow / doc-commit logic (the gitignored-path filter shipped separately).
- Choosing or provisioning the hermes model (operator-owned routing config).
- The coordinare-side resilience (spec-098 retry / ENV_BLOCKED — already shipped).
- The concrete deployment steps this enables (the hermes routing-table entry, the hermes-performer routing mount, the performer image rebuild) — configuration/ops, not code in this spec.
- Any change to the tool-call health probe or to any existing routed backend/pair.
