# Implementation Plan: Anthropic→OpenAI Request-Translating Shim

**Branch**: `084-anthropic-openai-translate` | **Date**: 2026-06-08 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/084-anthropic-openai-translate/spec.md`

## Summary

Add a third routing **strategy — `translate`** — to the 078 self-hosted backend layer so a
`(claude_code, <model>)` entry can reach an **OpenAI-wire** upstream (Ollama-direct gpt-oss)
with **no LiteLLM in the path**. `claude_code` speaks the **Anthropic** wire protocol
(`POST /v1/messages`), while self-hosted gpt-oss on Ollama speaks **OpenAI** wire only
(`/v1/chat/completions`). Today only LiteLLM bridges the two, and its *streaming*
harmony→tool_calls handling is broken (LiteLLM #17246 / #13300).

The technical approach reuses the 078 `SelfHostedShim` (an aiohttp loopback reverse proxy) and
its `_FilterChain` / `NORMALIZER_REGISTRY` SSE model unchanged, and inserts two new pure
translator modules: a **request translator** (Anthropic `/v1/messages` body → OpenAI
`/v1/chat/completions` body) and a **response translator** (OpenAI JSON + SSE → Anthropic wire),
the latter composing with the existing `harmony_tool_calls` / `strip_reasoning` normalizers. The
routing model (`routing.py`) gains the `translate` strategy value plus a load-time validator that
rejects contradictory wire/strategy combinations (FR-008). `launch.py` gains a `translate`
dispatch branch and `health.py` gains a full-round-trip probe (FR-010). Activation stays
per `(backend, model)`; every other pair is untouched (FR-007).

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; production on 3.14.5 via uv)
**Primary Dependencies**: pydantic 2.x (routing-table + target-descriptor models + load-time validation, reused from 078); aiohttp (the `SelfHostedShim` loopback reverse-proxy server, reused from 073/078/080); httpx (upstream client + health-probe client); structlog (observability). No new external dependencies anticipated.
**Storage**: N/A — no new persisted coordinare state. The routing table is a config surface (the `SELFHOSTED_ROUTING_CONFIG`-pointed YAML); translator/normalizer decisions are emitted as observability records to the job's existing `capture_dir`, the same mechanism `ClaudeCodeShim`/`SelfHostedShim` already use.
**Testing**: pytest via `.venv/bin/pytest`; ruff via `.venv/bin/ruff check`. Unit tests under `tests/unit/`; new translator/strategy tests follow the existing 078 proxy test layout. TDD Red-Green-Refactor; coverage must not decrease (Constitution II).
**Target Platform**: Linux server (the performer container; the shim binds `127.0.0.1:0` loopback inside it).
**Project Type**: single (the coordinare/performer Python package; no frontend/mobile split).
**Performance Goals**: **Parity-or-better end-to-end latency versus the current LiteLLM-shim qa path** for an equivalent qa exchange (streaming and non-streaming). The translator is an in-process pure transform on already-buffered SSE frames / JSON bodies; it MUST NOT add a second network hop beyond the single loopback→upstream forward the 078 shim already makes. **Measured budget (now in spec SC-006): in-process translation overhead ≤ 5 ms median / ≤ 15 ms p99 per request, excluding upstream/network time, via a deterministic suite benchmark.**
**Constraints**: Security invariants are hard (FR-011 / FR-078-10): auth tokens and request/response bodies MUST NOT be logged; `auth_env` holds the NAME of the env var, never the secret. Fail-closed on broken/missing table at job start (FR-009 / FR-078-5). Health gate MUST NOT fail open (FR-010). Chunk-boundary-safe SSE transform composing with the existing `_FilterChain` (FR-003).
**Scale/Scope**: One new strategy, two translator modules, ~3 touched existing files (`routing.py`, `shim.py` / `launch.py`, `health.py`). First beneficiary: the `qa` role; the live config flip is explicitly **out of scope** (a live-verified follow-on).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Evaluated against `.specify/memory/constitution.md` v1.1.0.

| Principle | Status | Notes |
|-----------|--------|-------|
| **I. Code Quality** | PASS | Reuses existing 078 patterns (frozen pydantic models, `_FilterChain`, `NORMALIZER_REGISTRY`); translators are pure functions with single responsibility. Conventional Commits + `084-…` feature branch from main. |
| **II. Testing (NON-NEGOTIABLE)** | PASS | Unit + contract tests for request/response/SSE translation and load-time validation; integration test exercises the full translate round-trip against a stub OpenAI-wire upstream. TDD Red-Green-Refactor; deterministic stub upstream (no live network in CI); behavior-named tests. Coverage must not decrease. |
| **III. UX Consistency** | N/A | Infrastructure layer; no end-user UI surface. Operator-facing surface is the routing-table YAML, which follows the existing 078 schema + actionable fail-fast errors (FR-009). |
| **IV. Performance by Design** | PASS | Spec **SC-006** now carries an explicit, measured budget — in-process translation overhead ≤ 5 ms median / ≤ 15 ms p99 per request (excluding upstream/network), verified by a deterministic suite benchmark against the stub upstream — plus the SC-001 end-to-end parity-or-better claim (the translate path removes the LiteLLM hop rather than adding one). The design adds no extra network hop, so this is a measurement/assertion obligation, satisfied by a benchmark task in `/speckit.tasks`. |
| **V. Clarity Before Action** | PASS | Spec checklist has 0 `[NEEDS CLARIFICATION]`; requirements checklist all `[x]`. No unresolved ambiguity blocks planning. |

**Gate result**: PASS. The earlier Principle IV flag (no measured latency budget) is resolved — spec
**SC-006** now states a deterministic, benchmarkable budget. No Constitution violation remains.

## Project Structure

### Documentation (this feature)

```text
specs/084-anthropic-openai-translate/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
│   ├── routing-table.md         # translate-strategy schema + load-time validation rules
│   ├── request-translation.md   # Anthropic /v1/messages → OpenAI /v1/chat/completions field map
│   └── response-translation.md  # OpenAI JSON + SSE → Anthropic wire field/event map
├── checklists/
│   └── requirements.md  # (pre-existing) spec quality checklist
├── spec.md              # (pre-existing) feature specification
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
agent/performer/src/performer/proxy/
├── routing.py                    # MODIFY: add "translate" to Strategy Literal;
│                                  #         extend _validate_strategy to reject
│                                  #         translate + wire_format == "anthropic" (FR-008)
├── shim.py                       # MODIFY: SelfHostedShim gains request-translate hook on the
│                                  #         inbound /v1/messages body and response-translate on the
│                                  #         OpenAI reply, composing with existing _FilterChain (FR-001/2/3)
├── launch.py                     # MODIFY: _launch_for_target dispatches strategy == "translate"
│                                  #         (claude_code → ANTHROPIC_BASE_URL points at the loopback shim)
├── health.py                     # MODIFY: translate-target probe exercises the full
│                                  #         translate-and-forward round trip; no fail-open (FR-010)
├── translate/                    # NEW package: pure Anthropic↔OpenAI translators
│   ├── __init__.py
│   ├── request.py                # NEW: Anthropic /v1/messages body → OpenAI /v1/chat/completions body
│   ├── response.py               # NEW: OpenAI JSON response → Anthropic /v1/messages response
│   └── sse.py                    # NEW: StatefulSSEFilter-based OpenAI-SSE → Anthropic-SSE event translator
└── normalizers/                  # UNCHANGED: harmony.py, reasoning.py, base.py, __init__.py reused as-is

tests/unit/
├── test_translate_request.py      # NEW: FR-001 request field mapping (system/messages/blocks/tools/...)
├── test_translate_response.py     # NEW: FR-002/FR-005 non-streaming response + finish_reason mapping
├── test_translate_sse.py          # NEW: FR-003/FR-005 streaming event sequence + chunk-boundary safety
├── test_translate_tool_roundtrip.py  # NEW: FR-004 tool_use out + tool_result back, harmony compose
├── test_routing_translate.py      # NEW: FR-006/FR-008 strategy value + contradictory-combo rejection
├── test_launch_translate.py       # NEW: FR-007 dispatch + no-regression on other pairs
└── test_health_translate.py       # NEW: FR-010 full-round-trip probe, fail-closed, reroute_upstream
```

**Structure Decision**: Single-project layout (the existing coordinare/performer Python package). The
translate capability lives entirely under the established 078 proxy package
(`agent/performer/src/performer/proxy/`). The three pure translators are isolated in a new
`translate/` sub-package so they are unit-testable in isolation against stub bodies/streams, with no
aiohttp or network dependency. The four existing files (`routing.py`, `shim.py`, `launch.py`,
`health.py`) are extended at their existing strategy-dispatch / validation / probe seams rather than
restructured — preserving the reroute/normalize semantics byte-for-byte (FR-006/FR-007). Tests live in
`tests/unit/` per project convention (`.venv/bin/pytest`).

## Complexity Tracking

> Fill ONLY if Constitution Check has violations that must be justified.

No Constitution **violation** requires justification. The previously-tracked Principle IV gap is now
**resolved**:

| Item | Why tracked | Resolution |
|------|-------------|------------|
| Principle IV — no explicit, measured performance budget in Success Criteria | Constitution IV mandates a measurable budget in Success Criteria AND a benchmark; spec SC-001 only said "parity-or-better" qualitatively | **RESOLVED** — spec **SC-006** now states a measured budget (in-process translation overhead ≤ 5 ms median / ≤ 15 ms p99 per request, excluding upstream/network) verified by a deterministic suite benchmark, alongside the SC-001 end-to-end parity claim. `/speckit.tasks` MUST emit a benchmark task asserting SC-006. |
