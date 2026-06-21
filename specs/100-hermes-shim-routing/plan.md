# Implementation Plan: Route the hermes (tech_writer) Backend Through the Self-Hosted Normalizer Shim

**Branch**: `100-hermes-shim-routing` | **Date**: 2026-06-21 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/100-hermes-shim-routing/spec.md`

## Summary

hermes (role tech_writer, the `documenting` stage) is a JSON-only role but is the one contract-bound backend EXCLUDED from the 078 self-hosted shim (`launch.py` `UNSUPPORTED_BACKENDS={"hermes"}`, no `PROVIDER_BASE_URL_ENV` entry). So a reasoning model's wrapped output (reasoning-channel leak, ```json fences, prose preamble) reaches hermes's strict JSON parser raw and fails → `malformed_output` → card blocked at documenting. The fix makes hermes routable through the existing normalize-mode `SelfHostedShim` (opt-in via the routing table), with the 099 completion health probe and a normalizer chain that yields clean parseable JSON:

- **launch.py:** remove hermes from `UNSUPPORTED_BACKENDS`; add `"hermes": "HERMES_BASE_URL"` to `PROVIDER_BASE_URL_ENV`; hand hermes a loopback base with a `/v1` prefix (its CLI appends `/chat/completions`, unlike junie's verbatim full path).
- **normalizers:** REUSE the junie/098 chain — `strip_control_chars` + `strip_reasoning`. A live probe of gpt-oss-via-LiteLLM confirmed the failing shapes are invalid control characters and empty/overloaded bodies (the exact 098 shapes), NOT markdown fences — so NO new normalizer is required.
- **health:** completion probe (099) — tech_writer is non-tool-calling.
- **default-safe:** no hermes routing entry ⇒ byte-for-byte unchanged; no other backend touched.

## Technical Context

**Language/Version**: Python 3.14 (project min 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing `proxy/launch.py` (`UNSUPPORTED_BACKENDS`, `PROVIDER_BASE_URL_ENV`, `_with_verbatim_wire_path`, `_launch_for_target` normalize branch); `proxy/shim.py` (`SelfHostedShim`, served front-door paths); `proxy/routing.py` `TargetDescriptor` (+ 099 `health_probe`); `proxy/health.py` (099 completion probe); `proxy/normalizers/` (`strip_reasoning`, control-char, envelope) + a JSON-extraction normalizer; the hermes backend (`backends/hermes.py`, `HERMES_BASE_URL` + custom-provider config). **No new external dependencies.**
**Storage**: none. Routing entry is a config attribute (the `SELFHOSTED_ROUTING_CONFIG` YAML). **No coordinare state, no schema change.**
**Testing**: performer proxy tests (`agent/performer/tests/unit/proxy/`) — hermes launch routing (eligible + loopback base + suffix), completion probe for hermes, the JSON-extraction normalizer, default-unchanged regression; hermes backend test that the normalized output parses.
**Target Platform**: performer container (hermes-ephemeral) + the in-container shim.
**Project Type**: single project (performer-side only; no coordinare change).
**Performance Goals**: one bounded startup probe (completion, ~seconds); per-request normalization is in-process pass-through.
**Constraints**: health stays the only fail-closed surface; normalizers fail-open; secret-free; opt-in/default-unchanged; no other backend/pair affected.
**Scale/Scope**: the hermes routing path only.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. Extends an existing mechanism (the shim) to one more backend: an allowlist change + a base-suffix case + reuse of 099 probe + one focused normalizer. No new deps/state.
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS. TDD: failing tests for hermes launch eligibility + loopback base/suffix, completion-probe gating for hermes, the JSON-extraction normalizer (fences/preamble/reasoning → clean JSON), and default-unchanged regression, before implementation.
- **III. User Experience Consistency** — PASS. Reuses the existing health gate (proceed/reroute/fail-closed), the `proxy.health` record, and the normalizer chain other backends use.
- **IV. Performance by Design** — PASS. One bounded probe; normalization is in-process; unchanged when not routed.
- **V. Clarity Before Action** — PASS. Failure reproduced live; injection points identified; the open detail (which normalizers fully yield clean JSON; the `/v1` base-suffix) is resolved in research.md.

No violations → Complexity Tracking omitted.

## Project Structure

### Documentation (this feature)

```text
specs/100-hermes-shim-routing/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── hermes-shim-routing.md
└── tasks.md            # /speckit.tasks — not created here
```

### Source Code (repository root)

```text
agent/performer/src/performer/proxy/
├── launch.py           # remove hermes from UNSUPPORTED_BACKENDS; PROVIDER_BASE_URL_ENV["hermes"]=
│                       #   "HERMES_BASE_URL"; loopback base gets a /v1 prefix for hermes (CLI appends
│                       #   /chat/completions) — distinct from junie's verbatim full-path suffix
├── normalizers/        # JSON-extraction normalizer (strip ```json fences / prose preamble → object);
│                       #   reuse strip_reasoning (#130) + envelope-complete in the hermes chain
└── (shim.py, routing.py, health.py — reused unchanged; 099 completion probe applies)

agent/performer/tests/unit/proxy/
├── test_launch*.py     # hermes eligible + correct loopback base (/v1) + default-unchanged
├── test_health*.py     # hermes completion-probe gating
└── test_normalizer_*   # JSON-extraction normalizer
```

**Structure Decision**: Single project, performer-side only. The coordinare does not change. Activation is config (routing entry + mount), not code.

## Phase 0 — Research

See [research.md](research.md): hermes eligibility (drop from UNSUPPORTED_BACKENDS + provider-env map); the `/v1` loopback-base prefix vs junie's verbatim full-path suffix; which normalizers actually yield clean JSON for a reasoning model's output (strip_reasoning alone vs a NEW JSON-extraction/fence-strip normalizer); completion probe reuse (099); default-unchanged guarantee.

## Phase 1 — Design & Contracts

See [data-model.md](data-model.md) (hermes eligibility + loopback-base entities; no persisted state), [contracts/hermes-shim-routing.md](contracts/hermes-shim-routing.md) (routed-vs-default behavior table, normalizer chain, gating invariants), and [quickstart.md](quickstart.md) (the documenting-block replayed as acceptance scenarios + the activation recipe).

## Phase 2 — Tasks

Created by `/speckit.tasks` (not here).
