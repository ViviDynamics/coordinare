# Feature Specification: Dual-Model Planner/Executor Orchestration

**Feature Branch**: `080-dual-model-orchestration`
**Created**: 2026-06-05
**Status**: Draft
**Input**: User description: "Dual-model planner/executor orchestration: a configurable per-performer proxy that pairs a thinking model with a tool-calling model within a single agent turn, plus root-level endpoints / model_endpoints / modes config catalogs that unify single-model, dual-model, self-hosted, and native-frontier model selection behind one performer-level mode reference."

**Supersedes**: the prior unbuilt planning specs `079-tool-emulation-shim` and `080-planner-executor-split` (last on branch `079-tool-emulation-shim` @ `3e5d854`, now closed). Those framed the work as a composing stack — a tool-emulation compatibility shim (079) under a *role-level, two-call* planner→executor split (080) orchestrated by coordinare. This spec instead places the split **inside a single agent turn** behind an in-container proxy, makes it **per-performer and multi-strategy** (`single`/`always`/`conditional`/`think_once`), and **unifies all model selection** through root-level config catalogs. The role-level approach was considered and set aside in favor of the proxy's finer-grained think/act interleave and transparent-to-any-CLI property. The separate `078-selfhosted-backend-shim` robustness concern (response normalization / reroute / health-gating) remains an independent future idea; this spec only inherits 073's log-hygiene and timeout/no-black-hole guarantees (FR-018/FR-019).

## Overview

The 077 multi-backend round established two facts: (1) on self-hosted OSS models, reasoning quality and tool-calling reliability rarely live in the *same* model — `gpt-oss:120b` reasons well but its harmony tool-call format is lossy through LiteLLM, while smaller models emit clean `tool_calls` but plan poorly; and (2) `claude_code` is thinking-gated and several backends only behave when their response stream is normalized.

This feature lets a performer **pair a thinking model with a tool-calling model within a single agent turn** — a planner/executor split — transparently to the agent CLI, via an in-container reverse proxy (the proven `ClaudeCodeShim` pattern from 073). It also **unifies all model selection** behind three root-level config catalogs (`endpoints`, `model_endpoints`, `modes`) that every performer references by a single `mode` name, covering single-model, dual-model, self-hosted, and native-frontier cases with one concept. The catalogs are flat named lists, designed to back dashboard management forms.

The orchestration runs entirely in the performer container and in our tested codebase — deliberately **off** the fragile LiteLLM transform path (077's root cause of most failures). Each model half is an independently-addressed upstream, so the thinking and tool halves can sit on different stacks (LiteLLM, Ollama-direct, vLLM, or a native vendor cloud).

## Clarifications

### Session 2026-06-05

- Q: How should the planner's output be injected into the tool/executor model's request each turn? → A: As a **system message** prepended to the real conversation ("Follow this plan: …"), keeping user/assistant turns intact.
- Q: What wire formats must the in-container proxy accept at its front door in 080? → A: **Whatever the configured backends actually emit** — the proxy supports every wire shape its target backends use (not a fixed two-format set); the plan phase enumerates them per backend.
- Q: Should 080 mandate observability of the orchestration (plan, decisions, timings)? → A: **Yes, full capture** — per-turn strategy decision, plan text, and think/act/classify outcomes + timings are captured so per-persona value is measurable.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Unified model catalogs with single-model & native-frontier modes (Priority: P1)

An operator declares serving endpoints, named model@endpoint pairs, and behavior `modes` once at the config root, then points each performer at a model setup with a single `mode:` reference. Today's single-model behavior — self-hosted *or* native frontier — is expressed as `strategy: single`, with no proxy and no added latency.

**Why this priority**: This is the foundation every other story builds on, and it is the migration that makes the config "obvious and simple." It delivers value alone: the same orchestration behavior as today, expressed through one clear, dashboard-ready reference model instead of inline per-performer model fields.

**Independent Test**: Convert all in-repo example configs to the catalog form; every performer that ran single-model before runs identically (same harness, same model, same auth/override), and a native-frontier performer (`kind: anthropic`/`openai`, `strategy: single`) runs with the harness's own client and no proxy.

**Acceptance Scenarios**:

1. **Given** a config with `endpoints`, `model_endpoints`, and a `strategy: single` mode referencing one self-hosted `model_endpoint`, **When** the performer runs, **Then** the agent CLI is pointed at the override endpoint directly, no proxy launches, and behavior matches the pre-080 single-model path.
2. **Given** a `strategy: single` mode whose `model_endpoint` resolves to an endpoint with `kind: anthropic`, **When** the performer runs, **Then** no provider override is set, the harness uses its native vendor client + vendor key, and no proxy launches.
3. **Given** a `mode` that references a non-existent `model_endpoint`, or an inline `model:` left on a performer, **When** config loads, **Then** loading fails with a clear error naming the dangling reference / removed field — never silently at card time.
4. **Given** a `strategy: single` mode that also sets `thinking:`, **When** config loads, **Then** validation rejects it (the shape cannot misrepresent intent).

---

### User Story 2 - Dual-model "always think, then act" (Priority: P2)

A performer is assigned a `strategy: always` mode that names a `thinking` model_endpoint and a `tool` model_endpoint. For every completion the agent CLI issues, an in-container proxy first runs the thinking model to produce a plan, then runs the tool-calling model (with that plan injected) to emit the actual `tool_calls`, and returns one recombined response in the CLI's wire format.

**Why this priority**: This is the core new capability and the simplest, most correct orchestration. It is the unit that proves the planner/executor proxy works end-to-end before adding adaptive strategies.

**Independent Test**: Point a fake agent CLI at the proxy with `strategy: always`; assert the thinking upstream is called, then the tool upstream is called with the plan injected, and the returned response carries the tool model's `tool_calls` plus the plan surfaced per `expose_plan_as`.

**Acceptance Scenarios**:

1. **Given** a `strategy: always` mode with distinct thinking and tool model_endpoints, **When** the CLI sends a completion, **Then** the thinking upstream is invoked (tools hidden) and its output is injected into the tool-upstream request before tool_calls are produced.
2. **Given** `expose_plan_as: thinking`, **When** the recombined response is returned, **Then** the plan appears as a `thinking`/`reasoning` channel appropriate to the CLI's format and the `tool_calls` come from the tool model.
3. **Given** the CLI requested a streaming (SSE) completion, **When** the proxy responds, **Then** the act phase is streamed to the CLI (plan events synthesized first when required) and the stream is wire-valid for the CLI's parser.
4. **Given** the thinking upstream times out and `on_think_error: fall_back_to_act`, **When** the turn runs, **Then** the proxy proceeds act-only, logs a warning, and still returns a valid response.

---

### User Story 3 - Conditional escalation via a cheap classifier (Priority: P3)

A performer uses `strategy: conditional`. A cheap classifier pre-pass scores each turn's difficulty; turns at or above a configurable threshold run the full think→act path, while easier turns go straight to the tool model — trading a small classifier call for avoided thinking-model latency on routine turns.

**Why this priority**: An optimization over `always` that only matters once the base dual-model path is proven. Higher misfire risk (the classifier can mis-score), so it lands after P1/P2.

**Independent Test**: Drive the proxy with stubbed classifier scores; assert turns ≥ threshold take think→act and turns < threshold take act-only, and that classifier failure defaults to think.

**Acceptance Scenarios**:

1. **Given** a classifier score ≥ `threshold`, **When** the turn runs, **Then** the think→act path executes.
2. **Given** a classifier score < `threshold`, **When** the turn runs, **Then** only the tool model is called.
3. **Given** the classifier upstream fails, **When** the turn runs, **Then** the proxy defaults to think (conservative) and logs the fallback.

---

### User Story 4 - Think once, act many (cached plan) (Priority: P3)

A performer uses `strategy: think_once`. The thinking model produces a plan on the first turn of a stage; subsequent turns reuse the cached plan with the tool model until the plan is invalidated by a turn-count budget or an error observed in incoming tool results, amortizing the thinking cost across the stage.

**Why this priority**: A second optimization profile for long, plan-stable stages. Independent of conditional; lands alongside P3.

**Independent Test**: Run a sequence of turns through one proxy process; assert the thinking model is called once up front, the cached plan is reused on following turns, and a re-think is triggered after `invalidate_after_turns` or when an error marker appears in the latest tool result.

**Acceptance Scenarios**:

1. **Given** the first turn of a stage, **When** it runs, **Then** the thinking model is called and its plan is cached in proxy memory.
2. **Given** a cached valid plan and turn N < `invalidate_after_turns`, **When** a turn runs, **Then** the thinking model is NOT called and the cached plan is injected into the tool-model request.
3. **Given** `invalidate_on_error: true` and the incoming request's latest tool result carries an error marker, **When** the next turn runs, **Then** the plan is re-thought.

---

### Edge Cases

- **CLI ↔ upstream format mismatch**: the CLI speaks Anthropic `/v1/messages` while the tool upstream is OpenAI-compatible (or vice versa) → the proxy normalizes through one internal `LLMTurn` representation; round-trips must preserve messages, tool schemas, and `tool_calls`.
- **Tool upstream failure**: there is no fallback (the tool call IS the answer) → surfaced on the existing single-model backend failure/retry path.
- **Mixed-stack mode**: `thinking` on a native vendor and `tool` on self-hosted → the proxy acts as a client to each upstream's wire format with its own auth; both halves time-bounded.
- **Reference cycles / partial catalogs**: a `mode` referencing a `model_endpoint` whose `endpoint` is undefined → load-time validation error.
- **Auth/secret leakage**: tokens and request/response bodies must never appear in logs (inherited 073 constraint).
- **Black-holed turn**: any upstream hang is bounded by a per-call timeout and composes with the 077 stall watchdog.

## Requirements *(mandatory)*

### Functional Requirements

**Config catalogs & unification (US1)**
- **FR-001**: Config MUST support a root-level `endpoints` list; each entry has `name`, `kind` (`litellm` | `ollama` | `vllm` | `openai` | `anthropic` | …), and connection fields (`base_url`/`auth_env` for self-hosted; vendor `auth_env` for native).
- **FR-002**: Config MUST support a root-level `model_endpoints` list; each entry has `name`, `endpoint` (a reference to `endpoints[].name`), and `model`.
- **FR-003**: Config MUST support a root-level `modes` list; each entry has `name`, `strategy` (`single` | `always` | `conditional` | `think_once`), and strategy-appropriate model_endpoint references (`tool`, optional `thinking`, optional `classifier`) plus strategy params.
- **FR-004**: Each performer MUST select its model setup with a single `mode:` reference; `backend` (harness) remains a separate performer field.
- **FR-005**: Loading MUST validate that every reference resolves (`performer.mode → modes → model_endpoints → endpoints`); a dangling reference is a loud load-time error.
- **FR-006**: Inline per-performer `model:` MUST be removed; its presence is a load-time error directing the operator to the catalog (hard cut, no dual-path).
- **FR-007**: An endpoint with a native `kind` (`anthropic`/`openai`/…) MUST cause the harness to use its own vendor client with no provider override; a self-hosted `kind` MUST set the provider-override env.
- **FR-008**: Strategy-specific fields MUST be validated against the strategy (e.g. a `single` mode with `thinking`/`classifier` set is rejected; an `always` mode missing `thinking` is rejected).

**Proxy & orchestration (US2–US4)**
- **FR-009**: When a performer's mode `strategy != single`, the system MUST launch an in-container reverse proxy bound to loopback and point the agent CLI at it; when `strategy == single`, NO proxy launches.
- **FR-010**: The proxy MUST accept whatever wire format(s) the configured target backends actually emit — not a fixed two-format set. At minimum this includes OpenAI `/v1/chat/completions` and Anthropic `/v1/messages`; the plan phase MUST enumerate the exact request shape per backend (e.g. OpenAI `/v1/responses` if a backend uses it) and the proxy MUST normalize each to the internal `LLMTurn` and render responses back to the originating CLI's format.
- **FR-011**: An `Upstream` client MUST render an `LLMTurn` into its endpoint's `kind` format and parse the response back, with independently-configured auth per upstream.
- **FR-012**: `strategy: always` MUST run think (tools hidden) then act (tool schemas present), then assemble one response. The planner's output MUST be injected into the act request as a **system message** prepended to the real conversation, leaving the existing user/assistant turns unchanged.
- **FR-013**: `expose_plan_as` MUST select how the plan is surfaced: `thinking` (format-appropriate reasoning channel) | `prepend_content` | `drop` (internal only).
- **FR-014**: The `ResponseAssembler` MUST support both JSON and SSE responses for each supported wire format; in SSE mode the think phase runs internally and the act phase is streamed (plan events synthesized first when required).
- **FR-015**: `strategy: conditional` MUST score each turn via a classifier upstream (or rule set) and run think→act at/above `threshold`, else act-only; classifier failure MUST default to think.
- **FR-016**: `strategy: think_once` MUST cache a plan per proxy process lifetime (one job/stage), reuse it across turns, and re-think when `invalidate_after_turns` is exceeded or (`invalidate_on_error`) an **error marker** appears in the latest incoming tool result. An *error marker* is defined as: the most recent tool-result message in the incoming request either (a) carries an explicit failure/error status field (e.g. `is_error: true`, non-zero exit code), or (b) has content matching the mode's configurable `error_pattern` regex (default `(?i)\b(error|exception|traceback|fatal|exit code [1-9])\b`). Detection MUST be case-insensitive and MUST NOT misfire on the plan text itself (only incoming tool results are scanned).
- **FR-017**: Thinking-upstream failure MUST honor `on_think_error` (`fall_back_to_act` default | `fail`); tool-upstream failure MUST surface on the existing backend failure/retry path with no fabricated output.
- **FR-018**: Every upstream call MUST be timeout-bounded and compose with the 077 stall watchdog; the proxy MUST never black-hole a card.
- **FR-019**: Auth tokens and request/response bodies MUST NOT appear in any log line; INFO logs are limited to method, path, upstream status, and latency (inherited from 073).
- **FR-020**: The agent's terminal contract (DONE / PARTIAL_PROGRESS / BLOCKED) MUST be unaffected — the proxy is transparent to job-level output.
- **FR-021**: The proxy MUST capture per-turn orchestration observability — the strategy decision taken (and, for `conditional`, the classifier score vs threshold), the planner's plan text, and the outcome + latency of each think/act/classify call — emitted in a form that lets per-persona/per-mode value be measured offline. This MUST respect FR-019 (no secrets/bodies in INFO logs); plan/timing capture goes to the job's durable artifacts, not INFO log lines.

### Key Entities *(include if feature involves data)*

- **Endpoint**: a model-serving location. `name`, `kind`, connection/auth. Distinguishes self-hosted (override/proxy-eligible) from native vendor (no override).
- **ModelEndpoint**: a named (model @ endpoint) pair. `name`, `endpoint` ref, `model`. The unit of model selection.
- **Mode**: a named orchestration behavior. `name`, `strategy`, model_endpoint refs (`tool`/`thinking`/`classifier`), and strategy params (`threshold`, `invalidate_after_turns`, `invalidate_on_error`, `expose_plan_as`, `on_think_error`).
- **DualModelProxy**: in-container loopback reverse proxy; transport shell that delegates to a strategy. Launched only for non-`single` strategies.
- **OrchestrationStrategy**: protocol (`single` | `always` | `conditional` | `think_once`) composing `think()` / `act()` / `assemble()`.
- **Upstream**: client for one model_endpoint; renders/parses `LLMTurn` ↔ the endpoint's wire format.
- **DifficultyClassifier**: cheap pre-pass used only by `conditional`; produces a difficulty score against a threshold.
- **ResponseAssembler**: merges think + act `LLMTurn`s into one wire-correct response (JSON & SSE, Anthropic & OpenAI).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of pre-080 single-model performers are expressible via the catalogs with identical runtime behavior (harness, model, auth/override) and no proxy.
- **SC-002**: A `strategy: always` performer completes a real card turn where the thinking model supplies the plan and the tool model supplies the executed `tool_calls`, verified in the captured orchestration artifacts (FR-021).
- **SC-003**: For `strategy: single`, added latency over the pre-080 path is zero (no proxy process, no extra model call).
- **SC-004**: Misconfigurations (dangling references, inline `model:`, strategy/field mismatches) are caught at config load with an actionable message in 100% of cases — never first surfacing mid-card.
- **SC-005**: No auth token or request/response body ever appears in proxy logs (asserted by test).
- **SC-006**: A dual-model performer never black-holes a card: every upstream stall is bounded and surfaces or falls back within the configured timeout.
- **SC-007**: All three multi-model strategies (`always`, `conditional`, `think_once`) and each supported wire format × both transports (JSON, SSE) are covered by passing tests.

## Future Work (out of scope for 080, sequenced after it)

- **Tool-call emulation for no-tools models** — a follow-on spec (the idea previously drafted as `079-tool-emulation-shim`, preserved at `3e5d854`). It would let a model lacking native tool support present a tool-calling interface by injecting the tool schema as text and parsing the reply back into `tool_calls`. **Deliberately sequenced after 080**, and planned using 080's evidence: if emulated tool-calls prove reliable enough for given personas, a no-tools reasoning model could serve as the `tool`/executor half directly — collapsing some dual-model `modes` back to a single cheaper call and saving time on those roles. 080 establishes the model-access seam and per-persona measurements that make this decision data-driven rather than speculative.
- **Self-hosted robustness layer** — response normalization / reroute / health-gating (`078-selfhosted-backend-shim`, parked on main). Independent of 080.
