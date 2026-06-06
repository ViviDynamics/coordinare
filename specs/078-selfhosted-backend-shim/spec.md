# Feature Specification: Self-Hosted Backend Robustness Layer

**Feature Branch**: `078-selfhosted-backend-shim`
**Created**: 2026-05-31 *(stub)* · **Formalized**: 2026-06-05
**Status**: Draft
**Input**: Design discussion during the 077 live round — "add a shim for each backend that's active when it's our own self-hosted infra running (as opposed to the agent's cloud API)." Formalized from the 078 stub.

## Overview

The 077 diverse-backend round surfaced a clear pattern: **almost every backend
failure was an artifact of *our self-hosted stack* (LiteLLM proxy → LM Studio /
Ollama → OSS models), not of the agent CLI itself.** Each agent works correctly
against its native cloud API; the failures appeared only when the backend was
re-pointed at our infra via its provider-override env.

This feature introduces a **self-hosted backend robustness layer**: a
normalization + routing + health-gating layer that activates **only when a
backend is routed at our self-hosted infra** (a provider-override base URL is
set) and is a transparent no-op when the backend uses its native vendor cloud
(where responses are already well-formed). It generalizes the existing
`ClaudeCodeShim` (073) — which already embodies this exact pattern (activates on
`LITELLM_PROXY_BASE_URL`, translates the response) — and extends the
in-container `DualModelProxy` reverse-proxy seam (080), which already
generalizes the ClaudeCodeShim transport.

## Clarifications

### Session 2026-06-05

- Q: How should a backend's self-hosted target (URL + strategy + normalizers) be declared in config? → A: A separate routing table keyed by `(backend, model)` maps to a target descriptor (base URL + strategy + normalizer keys), decoupled from per-backend config.
- Q: When the startup smoke-test fails for a self-hosted target, what is the default behavior? → A: Auto-reroute to the target's declared clean upstream if one exists; otherwise fail-closed (block the card with a clear error). Never fail-open.
- Q: How is the set of normalizers chosen for a given normalize-strategy target? → A: Explicitly — the operator lists normalizer keys per routing-table entry; only declared normalizers run (no auto-detection).

## Motivation — failure-mode catalog from 077 (all self-hosted-stack artifacts)

| Symptom (077) | Root cause | Current one-off fix |
|---|---|---|
| openclaw reviewer: "can't read files" / empty / garbage / `subprocess_exit:1` | LiteLLM's **streaming harmony→`tool_calls`** transform for gpt-oss leaks raw `<\|channel\|>commentary to=read …` text instead of structured `tool_calls` (LiteLLM #13300/#17246, openclaw PR #11210) | reroute openclaw → Ollama-direct (`gpt-oss:120b`), bypassing LiteLLM |
| claude_code thinking-block leakage | claude CLI surfaces `thinking` blocks from the proxy stream | `ClaudeCodeShim` (073) strips them (JSON + SSE) |
| hermes tech_writer can't connect | hermes-agent 0.15.2 config schema drift vs the backend's `providers:` block | per-backend config-schema fix (077) |
| codex implementer 62-min / 2-hr hangs | spark/qwen Ollama runner wedges; no client/role timeout | stall watchdog (077) + manual Ollama restart |
| claude_code qa `--max-tokens` crash | unsupported CLI flag for the self-hosted cap | env-var cap (077) |

Each was fixed ad-hoc. The thesis of 078: these belong to **one subsystem** —
the boundary between an agent CLI and our self-hosted model serving — and should
be handled there, once, rather than re-discovered per backend.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Harmony tool-call normalization for self-hosted gpt-oss (Priority: P1)

An operator routes a tool-using agent CLI (e.g. openclaw, codex) at a
self-hosted gpt-oss model served through LiteLLM. LiteLLM leaks raw
`<|channel|>commentary` harmony text instead of structured `tool_calls`, so the
agent "can't read files" and exits with garbage. With the robustness layer
active, a format-keyed `harmony → tool_calls` normalizer reassembles the leaked
text into structured tool calls (in both JSON and SSE responses) before the
agent sees them, so the same agent that fails today completes its lifecycle.

**Why this priority**: This is the highest-frequency, hardest-blocking 077
failure — it silently breaks every tool-using agent on self-hosted gpt-oss, the
most common OSS coding model in the fleet. It is also the one normalizer that is
shared across the most backends, so it delivers the most value per unit of work.

**Independent Test**: Point a tool-using backend at a self-hosted gpt-oss target
configured for `normalize`; feed a response containing leaked harmony
`commentary`-channel tool syntax (JSON and SSE) through the layer; assert the
agent receives well-formed `tool_calls` and successfully reads a file / edits.

**Acceptance Scenarios**:

1. **Given** a self-hosted gpt-oss target with the harmony normalizer enabled, **When** the upstream returns a non-streaming response with `<|channel|>commentary to=read` text, **Then** the layer emits structured `tool_calls` and the agent invokes the `read` tool successfully.
2. **Given** the same target with `stream: true`, **When** the upstream emits the harmony tool syntax across multiple SSE deltas, **Then** the layer reassembles and emits well-formed streaming `tool_calls` deltas with no leaked `<|channel|>` markers.
3. **Given** a backend pointed at its native vendor cloud (provider-override unset), **When** any response flows through, **Then** the layer is a transparent no-op and bytes pass unchanged.

---

### User Story 2 - Reroute around a broken middleware layer (Priority: P1)

An operator knows a clean upstream exists for a given model (e.g. Ollama parses
harmony → `tool_calls` correctly) where the LiteLLM path is lossy. Rather than
shimming the broken layer, they configure the backend+target to `reroute`:
point the backend directly at the clean upstream and skip LiteLLM entirely —
lower latency, no translation. This captures the openclaw → Ollama-direct 077
fix as a first-class, config-selectable strategy.

**Why this priority**: Co-equal P1 with Story 1 — the core design principle is
"a shim is not always the answer." Without reroute, every quirk must be shimmed
even when a clean path already exists, adding latency and translation risk where
none is needed. It is the actual fix that unblocked openclaw in 077.

**Independent Test**: Configure a backend+target with `strategy: reroute` to a
clean upstream base URL; assert the backend's provider env points at the clean
upstream (not LiteLLM) and no normalizer runs; assert a tool-calling probe
succeeds end-to-end.

**Acceptance Scenarios**:

1. **Given** a backend+target declaring `strategy: reroute` with a clean upstream URL, **When** the layer activates at job start, **Then** the backend's provider base URL is set to the clean upstream and the lossy middleware is bypassed.
2. **Given** a `reroute` target, **When** a completion flows through, **Then** no response normalizer is applied (reroute is not a shim).

---

### User Story 3 - Startup health/smoke-test gating (Priority: P2)

On container/daemon startup, the layer smoke-tests the resolved self-hosted path
(trivial connect + a tool-calling probe) and gates routing on the result, so a
broken path fails fast / reroutes / surfaces a clear error instead of
black-holing a card mid-lifecycle (cf. the 077 model-hang chase).

**Why this priority**: P2 — it is a robustness multiplier that turns a silent
mid-card stall into a fast, legible startup failure, but Stories 1 and 2 deliver
the core value (correct responses) on their own. Health gating composes with the
already-landed 077 stall watchdog rather than replacing it.

**Independent Test**: Start the layer against (a) a healthy tool-calling target
and (b) a target that fails the probe; assert healthy gates routing open and
unhealthy fails fast / surfaces a clear, actionable error before any card is
assigned.

**Acceptance Scenarios**:

1. **Given** a self-hosted target that passes the tool-calling smoke test, **When** the layer starts, **Then** routing is gated open and the job proceeds.
2. **Given** a self-hosted target that fails the smoke test **and declares a clean reroute upstream**, **When** the layer starts, **Then** the layer auto-reroutes to the clean upstream and the job proceeds.
3. **Given** a self-hosted target that fails the smoke test **with no declared reroute upstream**, **When** the layer starts, **Then** the job fails closed with a clear, specific error (not a mid-lifecycle black-hole) and does not silently accept the card.

---

### Edge Cases

- **Provider-override set but pointing at native cloud**: detection MUST treat "self-hosted-routed" as "override set" per current config semantics; a target explicitly marked native is a no-op even if a base URL is present.
- **Unknown response format / no matching normalizer**: the layer MUST pass the response through unchanged rather than corrupting it (fail-open on normalization, fail-closed only on health gating).
- **Partial / interleaved SSE harmony deltas**: the stateful SSE filter MUST buffer across chunk boundaries and not emit half-parsed tool calls.
- **Backend with no provider-override env mapping** (e.g. hermes): the layer cannot redirect it; it MUST surface a clear "cannot route" error rather than silently no-op into a broken path.
- **Health probe itself times out**: the smoke test MUST be timeout-bounded so a wedged upstream surfaces as "unhealthy" rather than hanging startup; an "unhealthy" result then follows the auto-reroute-then-fail-closed path (FR-078-5).
- **`(backend, model)` pair absent from the routing table**: treated as native-cloud — the layer is a no-op and bytes pass unchanged (FR-078-4).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-078-1**: The layer MUST be a no-op when a backend uses its native vendor cloud (provider-override unset), passing requests and responses through unchanged.
- **FR-078-2**: The shim MUST support pluggable normalizers keyed by response format / model-family (not by agent), reusable across backends — one harmony normalizer serves all tool-using agents on gpt-oss. The normalizers applied to a target MUST be those **explicitly declared** in that target's routing-table entry; the layer MUST NOT auto-detect or apply undeclared normalizers.
- **FR-078-3**: Normalizers MUST handle BOTH non-streaming JSON and SSE streams, using a stateful SSE filter that buffers across chunk boundaries (cf. `ClaudeCodeShim`).
- **FR-078-4**: Routing MUST be driven by a **routing table keyed by `(backend, model)`** that maps to a target descriptor — base URL, strategy (`normalize` | `reroute`), and, for `normalize`, the explicit normalizer keys. The table is a config surface decoupled from per-backend config; a `(backend, model)` pair with no entry is treated as native-cloud (no-op).
- **FR-078-5**: When a `(backend, model)` resolves to a self-hosted target, a startup tool-calling smoke-test **SHOULD** validate that path and **MUST** be timeout-bounded (a wedged upstream resolves to `unhealthy`, never a hung startup). The layer **MUST** gate routing on the probe outcome: on `healthy`, proceed; on `unhealthy` with a declared `reroute_upstream`, **auto-reroute** to it and proceed; on `unhealthy` with no declared reroute upstream, **fail-closed** (block the card with a clear, specific error) before any card is assigned. The layer **MUST NOT** fail-open onto an unhealthy path.
- **FR-078-6**: The implementation MUST generalize the existing `ClaudeCodeShim` (073) / `DualModelProxy` (080) seam into the shared framework rather than adding N independent shims.
- **FR-078-7**: The harmony→`tool_calls` normalizer MUST reassemble leaked `<|channel|>commentary` syntax into structured tool calls and MUST NOT leak raw harmony markers to the agent.
- **FR-078-8**: The thinking/reasoning-block-strip normalizer MUST remove reasoning blocks (claude, qwen reasoners) from both JSON and SSE responses, generalizing the 073 fix.
- **FR-078-9**: When normalization encounters an unknown format with no matching normalizer, the layer MUST pass the response through unchanged (fail-open) rather than corrupting it.
- **FR-078-10**: The layer MUST NOT log upstream auth tokens or request/response bodies; observability is limited to method/path/status/latency and normalizer/strategy decisions (inherited 073/080 constraint).

### Key Entities *(include if feature involves data)*

- **Routing table**: the config surface mapping `(backend, model)` → self-hosted target descriptor; decoupled from per-backend config. A missing entry means native-cloud (no-op).
- **Self-hosted target**: a resolved upstream (base URL + model + wire format) that a `(backend, model)` pair is routed at, carrying a `strategy` (`normalize` | `reroute`), for `normalize` an explicit set of normalizer keys, and optionally a declared clean reroute upstream used as the auto-reroute fallback on smoke-test failure.
- **Normalizer**: a pure, reusable transform keyed by response format / model-family (e.g. `harmony_tool_calls`, `strip_reasoning`), with a JSON path and a stateful SSE path.
- **Health/smoke-test result**: the outcome of the startup tool-calling probe for a target (healthy / unhealthy + reason), used to gate routing.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A tool-using agent on self-hosted gpt-oss that fails to read files in 077 completes a full card lifecycle (read → edit → tool-call) with the harmony normalizer active, in 100% of repeated runs of the regression fixture.
- **SC-002**: Zero raw `<|channel|>` / harmony markers and zero reasoning/thinking blocks reach the agent across the JSON and SSE regression fixtures.
- **SC-003**: With a backend on its native vendor cloud, byte-for-byte responses are identical with the layer enabled vs disabled (verified no-op).
- **SC-004**: A `reroute` target points the backend at the clean upstream and bypasses the lossy middleware, with no normalizer applied, verified by the routing assertion.
- **SC-005**: An unhealthy self-hosted target is detected at startup and, in 100% of probe-failure fixtures, either auto-reroutes to its declared clean upstream (when one exists) or fails closed with a clear error before any card is assigned — never black-holing mid-lifecycle and never failing open onto the unhealthy path.
- **SC-006**: A single shared harmony normalizer serves every tool-using backend on gpt-oss (no per-agent duplicate), demonstrated by reuse across at least two backends in tests.

## Out of scope

- Fixing LiteLLM itself (upstream — tracked separately via #13300/#17246).
- Changing native-cloud behavior of any backend.
- The 077 stall watchdog (already landed) — though it composes with the health-gating piece.
- **Mid-job liveness re-probing.** The health/smoke-test gate (FR-078-5) is a single-shot *startup* probe that gates routing before the card runs; it does not re-probe a healthy path mid-job. In-flight liveness is covered by two existing mechanisms it composes with: the shim's per-request forward timeout (a wedged upstream surfaces as a clean 502, including stalls that begin mid-stream via the inter-chunk read timeout) and the 077 stall watchdog. A periodic background liveness recheck is deliberately out of scope.
- **Salvaging residual harmony commentary text.** The harmony SSE filter reassembles `<|channel|>commentary to=…<|call|>` syntax into structured `tool_calls` and drops the surrounding non-call commentary prose. This is intentional: the agent CLI consumes `tool_calls`, not the model's free-text commentary channel, and forwarding that residue is what produced the 077 "garbage text" failure shape. Preserving/re-surfacing residual commentary text is out of scope.

## References

- 077 findings: `specs/077-multi-backend-qa/findings.md` (openclaw root cause + Ollama-direct fix; the full failure catalog).
- `agent/performer/src/performer/backends/claude_code_shim.py` (073) — the prototype.
- `agent/performer/src/performer/proxy/` (080 `DualModelProxy`) — the in-container reverse-proxy seam that already generalizes the ClaudeCodeShim; the natural home to extend.
- Upstream: BerriAI/litellm #13300, #17246; openclaw PR #11210.
