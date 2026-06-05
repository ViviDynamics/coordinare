# Phase 0 Research: Dual-Model Planner/Executor Orchestration

All spec `NEEDS CLARIFICATION` items were resolved in `/speckit.clarify` (see spec Clarifications). This document records the technical decisions that ground the plan, derived from the spec + a codebase integration scan.

## D1 — Orchestration locus: per-turn proxy (not coordinare role-level)

- **Decision**: Run the planner/executor split inside an in-container reverse proxy on the CLI→model-provider seam, intercepting each completion the agent CLI emits.
- **Rationale**: Transparent to every agent CLI; per-performer config maps to a per-container proxy; keeps orchestration logic in our tested codebase and off LiteLLM's transform path (077's root failure source); the two upstreams can be cross-stack.
- **Alternatives considered**: (a) Coordinare role-level two-call split (the prior `080-planner-executor-split` draft) — simpler and works today without a shim, but coarse (no per-turn interleave) and re-derives orchestration above the backend; **rejected** by explicit decision in favor of finer control. (b) LiteLLM-composed virtual model — **rejected**: compounds risk on the already-fragile proxy and is opaque/untestable.

## D2 — Reuse the `ClaudeCodeShim` reverse-proxy pattern

- **Decision**: Build `DualModelProxy` as an aiohttp reverse proxy bound to `127.0.0.1:0` (OS-assigned ephemeral port), modeled directly on `agent/performer/src/performer/backends/claude_code_shim.py` (lifecycle `start()` returns the loopback base URL; backend overwrites its provider base URL with it).
- **Rationale**: The shim already proves the exact seam, lifecycle, SSE handling, and log-hygiene constraints we need. Generalizing it is lower-risk than a new transport. The shim's `capture_dir` is the natural home for FR-021 observability.
- **Alternatives considered**: A separate process / sidecar — **rejected**: the same-process asyncio server is already proven and avoids IPC.

## D3 — Canonical internal representation (`LLMTurn`)

- **Decision**: Normalize every front-door request to an internal `LLMTurn` (messages, tool schemas, tool_calls, reasoning text); `Upstream` clients render/parse `LLMTurn` ↔ a specific wire format; `ResponseAssembler` renders the merged `LLMTurn` back to the originating CLI's format.
- **Rationale**: Avoids N×N format translation between {CLI format} × {thinking upstream format} × {tool upstream format}. Two adapters cover today's needs (Anthropic messages, OpenAI chat-completions).
- **Alternatives considered**: Direct format-to-format translation — **rejected**: quadratic and brittle.

## D4 — Per-backend wire format at the proxy seam

- **Decision**: The proxy speaks the format each CLI uses to call its provider base URL:
  - **claude_code** → Anthropic `/v1/messages` (already shimmed today).
  - **codex, opencode, junie, pi, openclaw** → OpenAI-style chat-completions via their provider configs (routed at LiteLLM today).
  - The plan/`/speckit.tasks` phase enumerates the exact request shape per backend before coding the adapter (FR-010); if any backend emits OpenAI `/v1/responses`, an adapter for it is added.
- **Rationale**: Matches the existing override mechanism per backend (`CODEX_PROVIDER_BASE_URL`, `JUNIE_PROVIDER_BASE_URL`, `PI_PROVIDER_BASE_URL`, `OPENCLAW_PROVIDER_BASE_URL`, `{PREFIX}_PROVIDER_BASE_URL` for opencode, `ANTHROPIC_BASE_URL` for claude_code).
- **Constraint surfaced**: **hermes has no provider-base-URL override** — it cannot be pointed at the proxy. hermes is limited to `strategy: single` in 080; adding hermes provider routing is out of scope (future).

## D5 — Plan injection as a system message

- **Decision**: Inject the planner's output into the act-phase request as a **system message** prepended to the real conversation ("Follow this plan: …"), leaving user/assistant turns unchanged (spec Clarification + FR-012).
- **Rationale**: Most portable across Anthropic/OpenAI formats; cleanly separates plan from task; doesn't fabricate assistant history.
- **Alternatives considered**: synthetic assistant turn / prepend-to-user — **rejected** (mutate history / blur plan vs task).

## D6 — Observability target

- **Decision**: Capture per-turn strategy decision (+ classifier score vs threshold for `conditional`), plan text, and per-call (think/act/classify) outcome+latency to the job's durable capture/artifact dir; INFO logs stay limited to method/path/status/latency with no secrets/bodies (FR-019/FR-021).
- **Rationale**: SC-002 verification and the data-driven tool-emulation follow-on both need the plan + timings; the `capture_dir` mechanism already exists on the shim.

## D7 — Config resolution & hard-cut migration

- **Decision**: Resolution chain `performer.mode → modes[] → model_endpoints[] → endpoints[]`, validated at load. Remove inline `performers.<role>.model`; its presence is a load error (hard cut). `strategy: single` reads only `tool:`. Endpoint `kind` (native vs self-hosted) decides override/proxy behavior.
- **Rationale**: One concept (`mode`) for all model selection; dashboard-form-ready; loud-at-load failure (Principle V). Confirmed with the user; no back-compat dual path (Principle I "no code for later").
- **Migration surface**: ~12 `config.example.*.yaml` files in repo root + the active gitignored operator configs (documented in quickstart).

## D8 — Performance budget (Principle IV)

- **Decision**: `single` = zero proxy overhead (no process); `DualModelProxy` non-model overhead < 50 ms/turn (benchmarked); `always` ≈ 2× single upstream latency (accepted, correctness-first, sequential Spark). Upstream calls serialized + timeout-bounded.
- **Rationale**: Constitution requires measurable budgets; sequential Spark makes concurrent think/act on one upstream counterproductive.
