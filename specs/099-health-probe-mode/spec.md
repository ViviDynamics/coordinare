# Feature Specification: Completion-Style Health-Probe Mode for Non-Tool-Calling Backends

**Feature Branch**: `099-health-probe-mode`  
**Created**: 2026-06-20  
**Status**: Draft  
**Input**: User description: "Completion-style health-probe mode for non-tool-calling self-hosted backends — unblock spec-098 US2 (junie assessor normalization)."

## Overview

The self-hosted robustness layer (spec 078) gates every routed model target at job start with a **startup health probe**. The probe is the layer's only fail-*closed* surface: an unhealthy target with no declared fallback causes the card to be rejected rather than black-holed mid-lifecycle. Today that probe is hardcoded as a **tool-calling** probe — it asks the model to emit a structured tool call and judges the target healthy only if a tool call comes back.

That works for the tool-using agents currently routed (the reviewer and the QA agent, which use a normalizer to reassemble the model's tool call before the probe judges it). But it makes it **impossible to route a non-tool-calling backend** through the layer. The **assessor** is a JSON-completion role: it runs its harness against a self-hosted model and reads a plain chat completion (it builds its assessment from the message content; it never tool-calls). The self-hosted model emits free-text content, never a structured tool call — so the tool-call probe always judges the assessor's target **unhealthy** and rejects every assessor card. That is strictly worse than leaving the assessor outside the layer entirely.

As a direct consequence, **spec-098 US2** — routing the assessor's flaky upstream through the layer's normalizers (strip stray control characters; promote a reasoning-only answer into content) so malformed responses are repaired before the strict parser sees them — cannot be turned on, even though those normalizers are already built, tested, and merged. The only thing blocking US2 is the probe's tool-call assumption.

This feature makes the health probe's **success criterion selectable per target**: keep the tool-call probe as the default (no change for any current target), and add a **completion probe** for non-tool-calling backends that judges health on a well-formed, non-empty completion — the same success shape the real client needs. With it, the assessor can be routed through the layer (gaining 098's normalization) and still be protected by an honest health gate.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A non-tool-calling target can be health-gated on a completion (Priority: P1)

An operator routes a non-tool-calling backend (the assessor) through the self-hosted layer by declaring its target with a **completion** probe mode. At job start the target is smoke-tested with a trivial completion request (no tools) and judged healthy when a normal, non-empty completion comes back — so the target is admitted instead of being wrongly rejected for "no tool call."

**Why this priority**: This is the whole point — without it the assessor cannot be routed through the layer at all, and 098's normalization stays dark. It is the MVP.

**Independent Test**: Point a completion-mode target at a source that returns a normal completion; verify the target is admitted (healthy → proceed). Point the same mode at a tool-only judgement and verify it is NOT wrongly required to tool-call.

**Acceptance Scenarios**:

1. **Given** a target declared with completion probe mode, **When** its upstream returns a normal non-empty completion, **Then** the target is judged healthy and the job proceeds.
2. **Given** a target declared with completion probe mode, **When** its upstream returns nothing / an empty body / a non-success response across the probe, **Then** the target is judged unhealthy and is gated (fail-closed, or auto-rerouted if a fallback upstream is declared) — never admitted onto a known-broken path.
3. **Given** a target with completion probe mode whose upstream returns only a reasoning channel with an empty answer, **When** the layer's normalizers would promote that reasoning into the answer, **Then** the probe judges health on the **normalized** result (healthy if a usable answer results), consistent with how the tool-call probe judges after its normalizer runs.

---

### User Story 2 - Existing tool-calling targets are completely unchanged (Priority: P1)

Every target that does not declare a probe mode keeps the current tool-call probe and its exact gating behavior. Operators who change nothing see no difference.

**Why this priority**: The probe is fail-closed; a regression here would reject healthy production work. Backward-compatibility is as important as the new capability. Same priority as US1.

**Independent Test**: Run the existing routed targets (reviewer, QA) with no config change; verify identical health decisions (healthy/unhealthy/rerouted/fail-closed) to before this feature.

**Acceptance Scenarios**:

1. **Given** a target with no probe mode declared, **When** it is health-checked, **Then** it uses the tool-call probe with byte-for-byte the same success criterion and gating as today.
2. **Given** a target that previously passed/failed the tool-call probe, **When** re-checked after this feature, **Then** the decision is unchanged.

---

### User Story 3 - The assessor's flaky upstream is normalized once routed (Priority: P2)

With the completion probe available, the operator routes the assessor through the layer so its responses pass through the layer's normalizers (control-character stripping + reasoning-promotion + envelope completion) before the strict harness parser sees them — activating spec-098 US2. Most flaky responses become clean; a persistently empty upstream is gated as an infrastructure condition rather than silently failing.

**Why this priority**: This is the payoff that motivated the feature, but it rides on US1+US2 and is realized by configuration + the already-merged 098 normalizers. Lower priority than the gating mechanism itself.

**Independent Test**: Route the assessor through a completion-mode normalize target; feed a response with control characters / an empty-but-reasoned answer; verify the harness receives a clean, parseable response and the card proceeds — the 098 US2 acceptance, now reachable.

**Acceptance Scenarios**:

1. **Given** the assessor routed through a completion-mode normalize target, **When** its upstream returns a response with control characters, **Then** the harness receives a parseable response (098 US2) and the card is not blocked on it.
2. **Given** the same routing, **When** the upstream is persistently empty across the health probe, **Then** the target is gated as unhealthy (infrastructure) rather than admitting the assessor onto a dead model.

---

### Edge Cases

- **No probe mode declared**: default tool-call probe, unchanged (US2).
- **Completion probe, clean completion**: healthy → proceed.
- **Completion probe, empty/garbled/non-success**: unhealthy → fail-closed (or auto-reroute if a fallback upstream is declared) — never fail-open onto a broken path.
- **Completion probe, reasoning-only answer**: judged on the normalized result, mirroring the tool-call probe's "normalize then judge" order.
- **Unknown/invalid probe mode value in config**: rejected at config-load (fail-fast), consistent with how unknown normalizer keys are rejected today — not a silent fall-through.
- **Timeout / wedged upstream**: bounded; surfaces as unhealthy, same as the tool-call probe.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: A self-hosted routing target MUST be able to declare its health-probe **mode**: the existing **tool-call** probe or a new **completion** probe. The selection is per target.
- **FR-002**: The completion probe MUST send a trivial, bounded chat-completion request carrying **no tools**, and MUST judge the target **healthy** when the response is a well-formed, non-empty, parseable completion — the same success shape a non-tool-calling client requires — and **unhealthy** otherwise.
- **FR-003**: The default (a target that declares no probe mode) MUST be the existing tool-call probe with **unchanged** success criterion and gating. The completion mode is strictly opt-in; no existing routed target's health decision may change.
- **FR-004**: The completion probe MUST run the response through the target's **declared normalizer chain before** judging health (so a reasoning-only answer the normalizers would promote counts as healthy), mirroring the tool-call probe's normalize-then-judge order.
- **FR-005**: The completion probe MUST remain **timeout-bounded** and **fail-closed** on a wedged, empty, or non-success upstream — admitting an unhealthy completion-mode target onto its path is forbidden, exactly as for the tool-call probe. Auto-reroute to a declared fallback upstream on unhealthy is preserved for both modes.
- **FR-006**: An invalid/unknown probe-mode value in the routing config MUST be rejected at config-load time (fail-fast), consistent with existing routing-config validation — never a silent default-through.
- **FR-007**: With a completion-mode target available, routing the assessor through a normalize target MUST enable spec-098 US2 (the assessor's upstream is normalized before its parser) without the tool-call false-negative — i.e. this feature is sufficient to make 098 US2 activatable by configuration alone.
- **FR-008**: The health decision record MUST carry only the probe **mode** plus the existing non-secret fields (method / path / status / resolved action) — never tokens or response bodies (carried invariant).

### Key Entities *(include if feature involves data)*

- **Health-probe mode**: a per-target selector — `tool_call` (default) or `completion` — that determines the probe request shape and the health success criterion. A config attribute of a routing target, not persisted coordinare state.
- **Health decision record**: the existing observability record for a probe outcome, extended with the probe **mode**; free of secret values and raw model output.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A non-tool-calling target declared with completion mode is admitted (healthy) on a normal completion, where today it would be wrongly rejected — 0% false-negative "no tool call" rejections for completion-mode targets.
- **SC-002**: 100% of existing routed targets (no probe mode declared) produce the **same** health decision as before this feature — no regression.
- **SC-003**: A completion-mode target whose upstream is persistently empty/broken is gated unhealthy (fail-closed or auto-rerouted) in 100% of cases — never admitted onto a dead path.
- **SC-004**: After this feature, the assessor can be routed through the layer and spec-098 US2's acceptance (control-char / empty-reasoned responses repaired before the parser) holds — by configuration, with no further code change.
- **SC-005**: 100% of health decision records carry the probe mode and 0% contain secret values or raw model output.
- **SC-006**: An invalid probe-mode value fails config-load with a clear error in 100% of cases (no silent fall-through).

## Assumptions

- The self-hosted layer already runs normalizers before judging the tool-call probe; the completion probe reuses that "normalize then judge" order (spec 078 + 098 normalizers, already merged).
- The routing table is a config surface (the operator-managed YAML); adding a per-target probe-mode attribute follows the existing target-descriptor + config-validation pattern.
- The assessor's self-hosted model emits free-text completions (not tool calls); a non-empty parseable completion is the correct, sufficient health signal for it.
- Auto-reroute on unhealthy (declared fallback upstream) already exists and applies to both probe modes unchanged.
- gpt-oss:120b model contention on the shared host is a contributing factor noted but not changed here (operator-owned).

## Dependencies

- Spec 078 (self-hosted robustness layer: health gating, normalizer chain, routing table) — the probe + gate this feature extends.
- Spec 098 (assessor resilience; US2 normalizers — control-char strip, reasoning-promote) — already merged; this feature makes US2 activatable for the non-tool-calling assessor.
- The operator-managed routing config (the `SELFHOSTED_ROUTING_CONFIG`-pointed YAML) — where the per-target probe mode is declared.

## Out of Scope

- Changing the tool-call probe's behavior, success criterion, or any existing routed target's decision.
- The assessor (junie) persona contract.
- Choosing/provisioning the assessor model or resolving shared-model contention (operator-owned).
- The spec-098 coordinare-side resilience (US1 retry-not-block, US3 empty-body→ENV_BLOCKED) — already shipped and live.
- Auto-detecting probe mode from the backend — it is an explicit per-target declaration, never inferred.
- The concrete deployment steps this feature enables (the assessor routing-table entry, mounting the routing config into the assessor performer, and rebuilding the performer image) — configuration/ops, not code in this spec.
