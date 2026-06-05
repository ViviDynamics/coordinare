# Phase 1 Data Model: Dual-Model Planner/Executor Orchestration

Two groups: **config catalogs** (coordinare, pydantic, validated at load) and **runtime orchestration types** (performer, in the proxy package). They share no module across the unit boundary; the proxy receives resolved values via the dispatch payload.

## Config catalogs (coordinare — `src/coordinare/config.py`)

### Endpoint
A model-serving location.

| Field | Type | Notes |
|---|---|---|
| `name` | str | unique key; referenced by `model_endpoints[].endpoint` |
| `kind` | enum: `litellm` \| `ollama` \| `vllm` \| `openai` \| `anthropic` | self-hosted kinds → provider override + proxy-eligible; native kinds (`openai`/`anthropic`) → no override, never proxied |
| `base_url` | str \| None | required for self-hosted kinds; MUST be absent/ignored for native kinds |
| `auth_env` | str \| None | name of the env var holding the token/key (never the secret itself) |

**Validation**: native `kind` MUST NOT set `base_url`; self-hosted `kind` MUST set `base_url`. `ConfigDict(extra="forbid")`.

### ModelEndpoint
A named (model @ endpoint) pair — the unit of model selection.

| Field | Type | Notes |
|---|---|---|
| `name` | str | unique key; referenced by `modes[].{tool,thinking,classifier}` |
| `endpoint` | str | MUST resolve to an `endpoints[].name` |
| `model` | str | model id as the endpoint expects it (e.g. `spark/gpt-oss:120b`, `claude-sonnet-4-5`) |

**Validation**: `endpoint` reference MUST resolve.

### Mode
A named orchestration behavior.

| Field | Type | Notes |
|---|---|---|
| `name` | str | unique key; referenced by `performers.<role>.mode` |
| `strategy` | enum: `single` \| `always` \| `conditional` \| `think_once` | |
| `tool` | str | ModelEndpoint ref; REQUIRED for all strategies (the executor / sole model) |
| `thinking` | str \| None | ModelEndpoint ref; REQUIRED for `always`/`conditional`/`think_once`; MUST be absent for `single` |
| `classifier` | str \| None | ModelEndpoint ref OR rule set; used ONLY by `conditional` |
| `threshold` | float \| None | `conditional` only; escalate when score ≥ threshold |
| `invalidate_after_turns` | int \| None | `think_once` only |
| `invalidate_on_error` | bool \| None | `think_once` only |
| `error_pattern` | str \| None | `think_once` only; regex for the FR-016 error marker; default `(?i)\b(error\|exception\|traceback\|fatal\|exit code [1-9])\b` |
| `expose_plan_as` | enum: `thinking` \| `prepend_content` \| `drop` | default `thinking`; multi-model strategies only |
| `on_think_error` | enum: `fall_back_to_act` \| `fail` | default `fall_back_to_act` |

**Validation** (FR-008): `single` MUST NOT set `thinking`/`classifier`/strategy params; `always`/`conditional`/`think_once` MUST set `thinking`; `conditional` MUST set `classifier` (+ `threshold`); all model_endpoint refs MUST resolve.

### PerformerRoleConfig (modified)
- **Remove** inline `model` (hard cut — presence is a load error pointing to the catalog; FR-006).
- **Add** `mode: str` — MUST resolve to a `modes[].name`.
- Keep `backend` (harness) and other operational fields.
- `resolved_role(role)` now resolves the full chain and returns the backend + the resolved mode (strategy + concrete endpoint/model/auth for each leg).

**Resolution chain**: `performer.mode → modes[] → model_endpoints[] → endpoints[]`. Any dangling link = load-time error (FR-005).

## Dispatch payload (coordinare → performer)

The resolved orchestration config crosses the unit boundary as a structured block on the existing Job API request. `resolved_role()` produces it; `_build_job_payload()` attaches it; the performer hands it to the proxy.

### OrchestrationConfig (new `JobInitPayload.orchestration` field, nullable)
Present only when `strategy != single` (absent → no proxy; FR-009).

| Field | Type | Notes |
|---|---|---|
| `strategy` | enum | `always` \| `conditional` \| `think_once` |
| `tool` | UpstreamRef | resolved `{base_url, model, wire_format, auth_env}` for the executor |
| `thinking` | UpstreamRef | resolved executor's planner leg |
| `classifier` | UpstreamRef \| RuleSet \| None | `conditional` only |
| `threshold` | float \| None | `conditional` only |
| `invalidate_after_turns` / `invalidate_on_error` / `error_pattern` | — | `think_once` only |
| `expose_plan_as` | enum | `thinking` \| `prepend_content` \| `drop` |
| `on_think_error` | enum | `fall_back_to_act` \| `fail` |

- **`UpstreamRef`** carries the *resolved* base_url + model + wire_format + the **name** of the auth env var — never the secret value. The token itself continues to flow through the existing `secrets` dict (unchanged mechanism), so no secret is added to a new payload field.
- This is a genuinely new payload shape (today's payload carries one backend/model); it is registered in `contracts/proxy-wire-contract.md`.

## Runtime orchestration types (performer — `performer/proxy/`)

### LLMTurn (canonical representation)
Format-neutral turn used internally.
- `messages: list[Message]` (role + content; content may include text/tool blocks)
- `tools: list[ToolSchema] | None`
- `tool_calls: list[ToolCall] | None` (on responses)
- `reasoning: str | None` (planner output / thinking)
- `stream: bool`

### Upstream
Client for one resolved model_endpoint.
- holds `{base_url, model, auth, wire_format}`
- `async complete(turn: LLMTurn, *, tools: bool) -> LLMTurn` — renders to its wire format, calls, parses back. `tools=False` hides tools (think phase).
- timeout-bounded (FR-018).

### OrchestrationStrategy (Protocol)
- `async run(turn: LLMTurn, *, thinking: Upstream | None, tool: Upstream, classifier: Classifier | None, ctx: StageState) -> LLMTurn`
- Implementations: `Single` (proxy not launched; documented for completeness), `AlwaysThinkThenAct`, `ConditionalEscalation`, `ThinkOnceActMany`.
- Compose shared helpers `think()`, `act()`, `assemble()`.

### StageState (think_once)
Per-proxy-process (one job/stage) state.
- `cached_plan: str | None`
- `turns_since_plan: int`
- `invalidate(reason)` — turn-count or error-marker triggered.

### DifficultyClassifier
- `async score(turn: LLMTurn) -> float` (model or rules); failure → default-think (FR-015).

### ResponseAssembler
- `assemble(plan: str | None, act: LLMTurn, *, expose_plan_as, cli_format, stream) -> response`
- Handles JSON and SSE for each supported `cli_format` (FR-014).

### OrchestrationRecord (FR-021 observability)
Written per turn to the job capture dir.
- `strategy`, `decision` (e.g. `escalated`/`act_only`), `classifier_score` (nullable), `plan` (text), and per-call `{phase, model_endpoint, latency_ms, ok}`. No secrets/bodies.
