# Implementation Plan: Self-Hosted Backend Robustness Layer

**Branch**: `078-selfhosted-backend-shim` | **Date**: 2026-06-05 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/078-selfhosted-backend-shim/spec.md`

## Summary

A normalization + routing + health-gating layer that activates **only when a
`(backend, model)` pair is routed at self-hosted infra** (a routing-table entry
exists) and is a transparent byte-for-byte no-op otherwise. It folds the five
ad-hoc 077 self-hosted-stack fixes into one subsystem at the agent-CLI ↔
self-hosted-serving boundary.

Technical approach: extend the existing in-container reverse-proxy seam
(`performer/proxy/`, home of 080 `DualModelProxy`, itself the generalization of
073 `ClaudeCodeShim`). Add (1) a **routing table** keyed by `(backend, model)`
→ target descriptor, resolved at job start; (2) a **normalizer registry** of
pure, format-keyed transforms (`harmony_tool_calls`, `strip_reasoning`) each
exposing a JSON path and a stateful SSE path; (3) a **shim transport** that
forwards to the self-hosted upstream and applies the target's explicitly-declared
normalizers; (4) a **reroute** strategy that just repoints the backend's
provider env at a clean upstream (no shim); and (5) a **timeout-bounded startup
smoke test** that gates routing with auto-reroute-then-fail-closed semantics.
Activation hooks the existing `maybe_launch_proxy(...)` seam at `main.py:~1107`,
parallel to 080.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: pydantic 2.x (routing-table + target descriptor models + validation), aiohttp (reverse-proxy server, reused from 073/080), httpx (upstream client + smoke-test probe), structlog (observability)
**Storage**: N/A — no new persisted coordinare state. Routing table is a config surface; orchestration observability (normalizer/strategy/health decisions) is written to the job's existing `capture_dir` (same mechanism `ClaudeCodeShim`/`DualModelProxy` already use).
**Testing**: pytest (`.venv/bin/pytest`); unit + contract tests under `agent/performer/tests/unit/proxy/`. Performer and coordinare suites run separately (conftest collision).
**Target Platform**: Linux container (ephemeral performer); same-process loopback proxy bound to `127.0.0.1:0`.
**Project Type**: single — extends the `performer` package; no new service.
**Performance Goals**: Native-cloud path is a **byte-for-byte no-op** with zero added hops (no routing-table entry → proxy not launched). Smoke test is **timeout-bounded** (no unbounded startup hang). `reroute` adds **zero translation latency** (direct repoint, no shim in path). `normalize` adds one same-host loopback hop, comparable to the existing 073 shim.
**Constraints**: MUST NOT log upstream auth tokens or request/response bodies (INFO = method/path/status/latency + normalizer/strategy/health decision only). Stateful SSE filter MUST buffer across chunk boundaries and never emit half-parsed tool calls. Never fail-open onto an unhealthy path; normalization fails *open* (pass-through) on unknown formats, health gating fails *closed*.
**Scale/Scope**: Fleet of ~6 routable backends × a handful of self-hosted models; 2 normalizers at launch (harmony, strip-reasoning); 2 strategies (`normalize`, `reroute`). Regression fixtures drive the JSON+SSE harmony/reasoning cases from the 077 catalog.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. Reuses the established proxy seam (one subsystem, not N shims per FR-078-6); normalizers are pure functions behind a small registry (interface-first, per project memory). No new architectural layer beyond extending `performer/proxy/`.
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS. Each normalizer gets unit tests on both JSON and SSE paths from 077-derived fixtures; routing-table resolution, reroute repoint, and smoke-test gating (pass / auto-reroute / fail-closed) get dedicated tests. Deterministic (fixtures + mocked upstreams via `httpx.MockTransport`, mirroring `test_dual_model_proxy.py`). No coverage decrease.
- **III. UX Consistency** — PASS. Operator-facing surface is one config routing table; failure modes surface as clear, specific startup errors (fail-closed), not mid-lifecycle black-holes.
- **IV. Performance by Design** — PASS. Measurable budgets are in Success Criteria: SC-003 byte-for-byte no-op on native cloud; SC-005 timeout-bounded probe with no mid-lifecycle black-hole; SC-004 reroute bypasses the lossy layer (no added translation).
- **V. Clarity Before Action** — PASS. All three clarification questions resolved in spec Session 2026-06-05; no `[NEEDS CLARIFICATION]` markers remain (requirements checklist all `[x]`).

**Result: PASS — no Complexity Tracking entries required.**

## Project Structure

### Documentation (this feature)

```text
specs/078-selfhosted-backend-shim/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── normalizer-wire-contract.md
│   └── routing-table.md
├── checklists/
│   └── requirements.md  # (already complete)
└── tasks.md             # /speckit.tasks output (NOT created here)
```

### Source Code (repository root)

```text
agent/performer/src/performer/proxy/
├── __init__.py
├── routing.py            # NEW — RoutingTable, TargetDescriptor models + (backend, model) resolution
├── normalizers/          # NEW — pure format-keyed transforms (registry)
│   ├── __init__.py       #   NORMALIZER_REGISTRY {key: Normalizer}
│   ├── base.py           #   Normalizer protocol: normalize_json(), SSE stateful filter
│   ├── harmony.py        #   harmony_tool_calls (gpt-oss; JSON + SSE reassembly)
│   └── reasoning.py      #   strip_reasoning (claude/qwen; generalizes 073)
├── shim.py               # NEW — SelfHostedShim transport (forward + apply declared normalizers)
├── health.py             # NEW — timeout-bounded smoke test + gating decision
├── launch.py             # EXTEND — maybe_launch_proxy resolves routing table, picks normalize|reroute, runs gate
├── dual_model_proxy.py   # (080, reused as the aiohttp reverse-proxy shell)
├── upstreams.py          # (080, reused: render/parse openai + anthropic wire formats)
└── claude_code_shim.py → backends/claude_code_shim.py  # (073, folded into normalizers/reasoning + shim)

agent/performer/tests/unit/proxy/
├── test_routing.py            # NEW — (backend, model) resolution; missing entry → no-op
├── test_normalizer_harmony.py # NEW — JSON + SSE harmony reassembly; no leaked markers
├── test_normalizer_reasoning.py # NEW — strip reasoning JSON + SSE (073 regression)
├── test_shim.py               # NEW — forward + declared-normalizer application; unknown format pass-through
├── test_health.py             # NEW — pass / auto-reroute / fail-closed / probe timeout
└── test_launch.py             # EXTEND — routing-table-driven launch, reroute repoint, no-op
```

**Structure Decision**: Single project, extending the existing `performer/proxy/`
package — the seam 080 already established for in-container reverse proxying. The
073 `ClaudeCodeShim` reasoning-strip behavior is folded into
`normalizers/reasoning.py` + the shared `shim.py` rather than left as a parallel
implementation (FR-078-6). No new service or top-level module is introduced.

## Complexity Tracking

> No Constitution Check violations — section intentionally empty.
