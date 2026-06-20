# Implementation Plan: Completion-Style Health-Probe Mode for Non-Tool-Calling Backends

**Branch**: `099-health-probe-mode` | **Date**: 2026-06-20 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/099-health-probe-mode/spec.md`

## Summary

The 078 self-hosted layer's startup health probe (`proxy/health.py check_health`) is hardcoded tool-call-only: it sends a `tools` request and gates a target healthy only if a structured `tool_call`/`tool_use` survives (`_has_structured_tool_call`). A non-tool-calling backend (the junie assessor on gpt-oss:120b Ollama-direct, which emits free-text content) therefore always gates **unhealthy → fail_closed**, blocking spec-098 US2 (routing junie through the normalizer chain).

The fix is a small, opt-in extension:

- Add a per-target **`health_probe`** selector to `TargetDescriptor` — `tool_call` (default, unchanged) or `completion`.
- For `completion`: send a trivial **no-tools** chat-completion probe and judge **healthy** on a well-formed, **non-empty** completion **after the target's normalizers run** (mirroring the existing normalize-then-judge order, so a reasoning-only answer the strip_reasoning/#130 normalizer promotes counts as healthy).
- `gate()` (proceed / auto-reroute / fail_closed) and the tool-call path are untouched; the default is byte-for-byte unchanged.
- The health decision record gains the probe `mode` (secret-free).

With this, routing the junie assessor through a `completion`-mode normalize target activates 098 US2 by config alone.

## Technical Context

**Language/Version**: Python 3.14 (project min 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing `proxy/health.py` (`check_health`, `gate`, `_probe_body`, `_probe_url`, `_has_structured_tool_call`, `_translate_round_trip_has_tool_use`); `proxy/routing.py` `TargetDescriptor` (frozen pydantic, `Literal` types, `extra="forbid"`); the `NORMALIZER_REGISTRY` chain (078 + 098). **No new external dependencies.**
**Storage**: none. The probe mode is a config attribute of a routing target (the `SELFHOSTED_ROUTING_CONFIG` YAML). **No coordinare state, no schema change.**
**Testing**: pytest (performer proxy suite `agent/performer/tests/unit/proxy/`) — completion-probe health (healthy on non-empty completion, unhealthy on empty/non-200/timeout, normalize-then-judge), default-unchanged regression, config-validation fail-fast.
**Target Platform**: performer container (the probe runs in-container at job start).
**Project Type**: single project (performer-side only; no coordinare change).
**Performance Goals**: one extra-light probe at job start, same bound as today; no per-request cost.
**Constraints**: health stays the only fail-closed surface; normalizers stay fail-open; secret-free decision record; default tool-call path unchanged.
**Scale/Scope**: the self-hosted health-probe layer only.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. One new opt-in `Literal` field + one branch in `_probe_body`/`check_health` + one helper (`_has_nonempty_completion`), mirroring the existing tool-call helpers. No new deps, no new state.
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS. TDD: failing tests for completion-probe healthy/unhealthy/normalize-then-judge, default-unchanged regression, and invalid-mode config fail-fast, before implementation.
- **III. User Experience Consistency** — PASS. Reuses the existing `gate()` proceed/reroute/fail-closed surface + the existing `proxy.health` observability record (adds `mode`).
- **IV. Performance by Design** — PASS. Same single bounded probe; completion mode is lighter (no tools).
- **V. Clarity Before Action** — PASS. The blocking failure was reproduced live (098 US2 activation); injection points identified; the one design choice (separate field vs overloaded value) is resolved in research.md.

No violations → Complexity Tracking omitted.

## Project Structure

### Documentation (this feature)

```text
specs/099-health-probe-mode/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── health-probe-mode.md
└── tasks.md            # /speckit.tasks — not created here
```

### Source Code (repository root)

```text
agent/performer/src/performer/proxy/
├── routing.py    # add TargetDescriptor.health_probe: Literal["tool_call","completion"] = "tool_call"
│                 #   (frozen + extra="forbid" → invalid value fails config-load, FR-006)
└── health.py     # _probe_body: no-tools body for completion mode; new _has_nonempty_completion
                  #   (run normalizers then check content); check_health branch on mode; log mode

agent/performer/tests/unit/proxy/
├── test_health.py / test_health_completion.py  # completion-probe health + default regression
└── (routing/config validation test)            # invalid health_probe value fails fast
```

**Structure Decision**: Single project, performer-side only. The coordinare does not change. The probe mode is a config attribute consumed entirely within the performer's startup health gate.

## Phase 0 — Research

See [research.md](research.md): separate `health_probe` field vs overloading an existing value; the completion success criterion (non-empty content after normalizers) and why it mirrors the tool-call normalize-then-judge order; how the no-tools probe body is shaped per wire-format/strategy; fail-fast validation via the frozen `Literal` field.

## Phase 1 — Design & Contracts

See [data-model.md](data-model.md) (the `health_probe` enum on `TargetDescriptor`; no persisted state; the decision record gains `mode`), [contracts/health-probe-mode.md](contracts/health-probe-mode.md) (mode → probe body + success criterion + gating table + invariants), and [quickstart.md](quickstart.md) (the junie-assessor activation replayed as acceptance scenarios).

## Phase 2 — Tasks

Created by `/speckit.tasks` (not here).
