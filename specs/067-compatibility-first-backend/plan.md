# Implementation Plan: Compatibility-First Performer Backend

**Branch**: `067-compatibility-first-backend` | **Date**: 2026-05-20 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/067-compatibility-first-backend/spec.md`

## Summary

Add a new performer backend whose request payload is the lowest-common-denominator (LCD) of `/v1/chat/completions` — no server-side tool descriptors, no `developer` role, no `prompt_cache_key`, no other OpenAI-only fields — so that operators pointing the daemon at a self-hosted OpenAI-compatible endpoint (LM Studio, vLLM, Ollama, LiteLLM) can move a card from `TODO` to `IN_REVIEW` without source patches. The existing `codex` backend stays unchanged for OpenAI-targeted deployments; backend selection remains a one-line `config.yaml` flip. Upstream HTTP status/body MUST be surfaced verbatim through `http_performer_service` and `handle_system_error` so transient/permanent classification stops relying on substring-matching friendly-wrapped error strings.

Technical approach (from Phase 0 research): the compatibility backend is implemented as a new performer adapter named `opencode_compat` that mirrors `OpenCodeAdapter`'s one-shot CLI invocation pattern (per [[feedback_junie_own_harness]] — no subclassing). Tools execute agent-side via opencode's native function-calling protocol; the adapter emits a configuration that disables opencode's hosted-tool descriptors and forces `system` for any `developer`-coded prompts. Error transparency is added at the `http_performer_service` layer where a new `UpstreamHTTPError` envelope replaces the current generic exception.

## Technical Context

**Language/Version**: Python 3.11 (coordinare + performer)
**Primary Dependencies**:
- Performer: `httpx` (already a transitive dep), `psutil`, `structlog`, `pydantic` v2 (all existing)
- Harness: opencode CLI (already vendored in performer image; same binary used by `OpenCodeAdapter`)
- No new third-party packages required.
**Storage**: N/A — backend is stateless beyond opencode's per-session CLI state, identical to other adapters.
**Testing**: pytest with the existing `tests/integration/test_06*_*.py` pattern. New marker `lmstudio` gated by `LMSTUDIO_AVAILABLE` env var so CI without a local model still runs.
**Target Platform**: Linux container (performer image) + macOS dev (operator quickstart). Endpoint targets: OpenAI `/v1`, LM Studio `/v1`, vLLM `/v1`, Ollama `/v1`, LiteLLM proxy `/v1`.
**Project Type**: Single project (coordinare) + performer agent (shipped together as one repo).
**Performance Goals**: NFR-001 — first-token latency on compat backend vs codex on the same OpenAI endpoint within 10%. Measured at plan time via `tests/integration/test_067_perf_compat_vs_codex.py` (skipped when no OpenAI key).
**Constraints**:
- LCD request body: tool entries restricted to `type: function`; no `developer` role; no `prompt_cache_key`; no `response_format` extensions beyond `json_object`.
- Debug log redaction: secrets-bearing fields (`OPENAI_API_KEY`, `.env.example` contents echoed back in tool args) MUST be redacted before NFR-002 debug-logging emits a full request body.
- Adapter-per-harness: do not subclass `OpenCodeAdapter` — duplicate the small surface (per [[feedback_interface_first_design]] and [[feedback_junie_own_harness]]).
**Scale/Scope**: One new adapter (~400–500 LoC, mirroring `opencode.py` length), 2 coordinare-side touch points (http_performer_service envelope + handle_system_error classification), 1 new integration test file, 1 quickstart doc.

**Dispatch payload**: 067 adds **no** new fields to the coordinare → performer dispatch payload. The new `compat_remap_developer_role` flag is performer-local — read by the adapter directly from its loaded `config.yaml` at process start, not dispatched per-card. No entry in `specs/contracts/dispatch-payload.md` is required.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | ✅ PASS | New adapter follows existing `backends/` patterns; types on all public surface; no dead code (legacy adapters untouched). |
| II. Testing Discipline | ✅ PASS | Unit tests for LCD request shaping (table-driven assertions on request body), integration test against LM Studio gated by env var, contract test for `UpstreamHTTPError` schema. Coverage threshold not regressed. |
| III. UX Consistency | ✅ PASS | Operator UX surface is `config.yaml` (one-line change) + log output. Quickstart doc covers happy path. Error messages now include verbatim upstream body (FR-004), which is a strict UX improvement over today's "high demand on the model" wrapper. |
| IV. Performance by Design | ✅ PASS with documented exception | NFR-001 budget defined and benchmark planned (`test_067_perf_compat_vs_codex.py`), but operator-run rather than CI-gated. Justification recorded in `spec.md` NFR-001 "CI enforcement" subsection; a CI lane requires either a self-hosted LM Studio runner or per-PR OpenAI spend approval and is deferred to a follow-up spec. Adapter does not add a network hop or serialization layer beyond opencode's. |
| V. Clarity Before Action | ✅ PASS | All five Open Questions from spec are resolved in `research.md` (see Phase 0). No `NEEDS CLARIFICATION` tags remain. |

**Post-Phase-1 re-check**: see end of `research.md` and `data-model.md` — no new violations introduced.

## Project Structure

### Documentation (this feature)

```text
specs/067-compatibility-first-backend/
├── plan.md              # This file (/speckit.plan output)
├── research.md          # Phase 0 — Open Question resolutions + harness spike
├── data-model.md        # Phase 1 — LCD request payload + UpstreamHTTPError envelope
├── quickstart.md        # Phase 1 — operator-facing LM Studio runbook
├── contracts/
│   └── upstream_http_error.md   # Phase 1 — coordinare↔performer error envelope schema
├── spec.md              # Source feature spec
└── tasks.md             # Phase 2 — generated by /speckit.tasks (NOT created here)
```

### Source Code (repository root)

```text
agent/performer/src/performer/
├── backends/
│   ├── __init__.py                  # NEW key in supported_backends: "opencode_compat"
│   ├── opencode_compat.py           # NEW — LCD adapter; mirrors opencode.py shape, does NOT subclass
│   ├── opencode.py                  # unchanged
│   ├── codex.py                     # unchanged
│   └── ...
├── main.py                          # backend-selection wiring already generic — no change beyond registry
└── protocol.py                      # extend PerformerResponse.metrics with upstream_http_error (optional)

src/coordinare/
├── services/
│   └── http_performer_service.py    # surface UpstreamHTTPError envelope (status, body, route)
└── graph/nodes/
    └── handle_system_error.py       # classify by HTTP status, not substring of wrapped strings

config.example.yaml                  # document backend: opencode_compat + worked example
packages/service_inference/src/coordinare_service_inference/prompt.py
                                     # audit for web_search references assuming hosted execution

tests/
├── unit/
│   ├── test_067_compat_request_shape.py        # NEW — LCD payload assertions
│   └── test_067_upstream_error_classification.py  # NEW — transient/permanent by status
├── integration/
│   ├── test_067_env_bootstrap_lmstudio.py      # NEW — gated by LMSTUDIO_AVAILABLE
│   ├── test_067_perf_compat_vs_codex.py        # NEW — NFR-001 budget; skipped without OPENAI_API_KEY
│   └── test_067_swap_test.py                   # NEW — SC-003 codex↔compat flip
└── contract/
    └── test_067_upstream_http_error_schema.py  # NEW — envelope schema round-trip

docs/
└── quickstart-selfhosted.md         # NEW — points operators at LM Studio happy path
```

**Structure Decision**: Single project layout — coordinare + performer live in one repo and ship together. The new adapter lives in `agent/performer/src/performer/backends/` alongside the existing per-harness adapters, with no shared base beyond the existing `BackendAdapter` Protocol in `backends/base.py`. Coordinare-side changes are confined to two files (`http_performer_service.py` and `handle_system_error.py`) — the LangGraph topology and node set are untouched (out of scope per spec).

## Complexity Tracking

> No constitution violations to justify. Section intentionally empty.
