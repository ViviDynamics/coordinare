# Feature Specification: Compatibility-First Performer Backend

**Feature Branch**: `067-compatibility-first-backend`
**Created**: 2026-05-20
**Status**: Draft
**Input**: Surfaced during 065 live-validation against self-hosted models. The codex backend emits OpenAI-shaped requests (`type: web_search` server-side tool descriptors, `developer` role, `prompt_cache_key`, hosted `file_search`) that local OpenAI-compatible servers (LM Studio, vLLM, Ollama, LiteLLM) either reject or silently warn-and-drop. The env_bootstrap loop fails on these servers before a single card can move, blocking validation of the entire 065 self-hosted story. The operator's stated principle — verbatim — is "I want these performers to be highly compatible if possible."

## Background

The coordinare dispatches work through a performer (agent harness) that talks to a model via an OpenAI-compatible HTTP API. Today the only supported harness is **codex**, which is built against OpenAI's hosted API and freely uses features only the OpenAI endpoint implements:

- **Server-side tools**: `web_search`, `file_search` are sent as tool descriptors with no executor — OpenAI runs the tool, the local server cannot.
- **Reserved roles**: `developer` role in chat messages — local servers rewrite to `system` (with a warning) or refuse.
- **OpenAI-only request fields**: `prompt_cache_key`, response-format extensions, and other fields that LM Studio explicitly logs as "ignored."
- **Friendly-wrapped errors**: upstream context-length, rate-limit, and 5xx errors get rewrapped as "high demand on the model, retrying" strings that mask the real failure (this hid the LM Studio `n_keep ≥ n_ctx` failure that blocked env_bootstrap for hours).

The result is a deployment cliff: a config that points codex at OpenAI works; the same config pointed at a self-hosted endpoint runs but produces silent degradation or hard failures with unactionable logs. Operators cannot make the leap from "I have a local model" to "the daemon picks up a card" without source-code-level investigation.

The user's roadmap requires self-hosted open-weight models as a first-class deployment target. The cost of "codex + OpenAI works only" is acceptable for the codex backend. The cost of "no backend at all works against self-hosted" is not. This spec defines a compatibility-first backend that runs against the **lowest-common-denominator (LCD)** OpenAI-compatible API and surfaces upstream errors transparently.

Reasonable backend candidates: **opencode** (all tools agent-side by design, no hosted tool types), claude_cli-style one-shot harnesses, junie (already adapted in 065), or a thin in-tree harness purpose-built for the LCD profile. The spec is backend-agnostic — it sets the requirements; implementation chooses the harness.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Operator Points Daemon at a Self-Hosted Model and It Works (Priority: P1)

An operator has LM Studio (or vLLM, or Ollama, or a LiteLLM proxy in front of any of them) running a Qwen3/Llama3/equivalent open-weight model with sufficient context. They set `backend: <compatibility-first-backend>` and `base_url` to the local server. The first card flows through env_bootstrap → assessor → architect → implementer → reviewer → qa → closer without operator intervention or source-code patches.

**Why this priority**: This is the explicit motivation. Without it, the 065 self-hosted deliverable cannot be validated end-to-end. Every other priority depends on the env_bootstrap loop completing against a non-OpenAI endpoint.

**Independent Test**: Stand up LM Studio with `qwen3.6:35b` at `n_ctx >= 32768`. Configure `performer_endpoints[0].env.OPENAI_API_KEY` to point at the LM Studio endpoint. Pick a representative website card. Observe: env_bootstrap completes, services manifest emits, no `developer`-role warnings in the LM Studio console, no "unsupported tool type" warnings, no `prompt_cache_key ignored` warnings. Card reaches `IN_REVIEW`.

**Acceptance Scenarios**:

1. **Given** a self-hosted OpenAI-compatible endpoint with no support for hosted tools, **When** the daemon dispatches an env_bootstrap turn, **Then** the request body contains no tool descriptor of `type: web_search` or `type: file_search`, no `developer`-role message, and no `prompt_cache_key` field.
2. **Given** the same endpoint returns an HTTP 400 with body `{"error": "context length exceeded"}`, **When** the performer surfaces the error, **Then** the daemon log contains the verbatim upstream error body and HTTP status, not a "high demand on the model" wrapper.
3. **Given** the model emits a tool call for an agent-side tool (`read_file`, `grep_repo`, etc.), **When** the performer executes it, **Then** the performer (not the model server) runs the tool and feeds the result back as a normal `tool` role message.

### User Story 2 — Existing codex+OpenAI Deployment Keeps Working (Priority: P1)

The current `codex` backend stays available and unchanged for operators on OpenAI's hosted API. Switching backend is a one-line config change, not a fork.

**Why this priority**: 065 went to production with codex+OpenAI. Breaking it would erase that work. The compatibility-first backend is additive.

**Independent Test**: Existing 065 integration tests (`tests/integration/test_065_*.py`) pass unchanged. A config with `backend: codex` and an OpenAI base URL produces byte-identical request payloads to pre-067.

**Acceptance Scenarios**:

1. **Given** `backend: codex` is configured, **When** the daemon dispatches any turn, **Then** behaviour is identical to pre-067 (server-side tools allowed, `developer` role allowed, `prompt_cache_key` allowed).
2. **Given** `backend: <compat>` is configured against OpenAI's hosted API, **When** the daemon dispatches a turn, **Then** the card still completes — the LCD profile is a subset of OpenAI's capabilities, not incompatible with them.

### User Story 3 — Operator Diagnoses a Local-Server Failure From the Logs Alone (Priority: P2)

When the local server returns an error, the operator can read the daemon log and identify the root cause without attaching strace, tcpdump, or the LM Studio console. Upstream status codes, response bodies, and timing are passed through.

**Why this priority**: The 065 env_bootstrap failure took hours to diagnose because the "high demand on the model" wrapper hid an `n_keep: 11073 >= n_ctx: 4096` error that was sitting in the LM Studio console the whole time. Transparent surfacing is what makes the rest of the spec maintainable.

**Independent Test**: Configure a deliberately-broken backend (wrong base URL, expired API key, model not loaded). Inspect the daemon log. The error message must name the root cause (HTTP code + upstream body) within one log line at WARN or ERROR level.

**Acceptance Scenarios**:

1. **Given** the local server returns HTTP 404 with body `{"error": "model not found"}`, **When** the daemon logs the failure, **Then** the log line contains `"status": 404` and `"upstream_body": "model not found"`.
2. **Given** the local server returns HTTP 503, **When** the daemon classifies the error, **Then** it is treated as transient (existing `github_retry`-style backoff) and the upstream body is included in the warning.

## Requirements

### Functional Requirements

- **FR-001**: A new backend value `opencode_compat` (resolved in research.md R2) MUST be selectable via `config.yaml`'s `performers.<role>.backend` field. The existing `codex` value MUST continue to work with no behaviour change.
- **FR-002**: When the compatibility backend is selected, the request payload sent to the model endpoint MUST NOT contain: (a) tool entries whose `type` is anything other than `function`, (b) a message with `role: developer`, (c) the `prompt_cache_key` field, (d) any other field that is OpenAI-specific and not part of the `/v1/chat/completions` baseline that LM Studio, vLLM, and Ollama all implement.
- **FR-003**: All tools (read_file, grep_repo, list_dir, which, probe_version, web_search if needed, etc.) MUST execute on the agent side. "Agent side" means the performer process, not the model server. The model emits a function-call request; the performer executes the function; the performer feeds the result back as a `role: tool` message. There MUST be no path that ships a tool descriptor expecting the server to execute.
- **FR-004**: When the model endpoint returns a non-2xx HTTP response, the performer MUST surface to the coordinare: the HTTP status code, the response body (truncated to a known cap — proposed 2 KiB), and the request route. The coordinare MUST log these verbatim. Wrappers like "high demand on the model" MUST NOT replace the upstream body in logs.
- **FR-005**: Transient/permanent classification MUST be based on HTTP status (5xx → transient, 4xx → permanent, with documented exceptions for 408/429) and well-defined upstream error markers, not on substring matching of friendly wrapped strings. Where substring matching is necessary as a fallback, the marker list MUST be a single named constant referenced from one location.
- **FR-006**: The compatibility backend MUST work against LM Studio (the reference endpoint, CI-tested per FR-008). vLLM, Ollama, and LiteLLM proxy MUST also work without per-endpoint code paths but are validated by hand against the swap test (SC-003); CI gating against them is out of scope for 067. Portability is guaranteed structurally by the LCD outbound validator (`_assert_lcd_payload`), not by per-endpoint integration tests.
- **FR-007**: Backend selection MUST be orthogonal to model-provider selection. `backend: <compat>` with `base_url: https://api.openai.com/v1` MUST work; `backend: codex` with `base_url: http://localhost:1234/v1` MAY continue to fail and that failure MUST be documented (codex is the OpenAI-only fast path; compat is the portable path).
- **FR-008**: The env_bootstrap pipeline (service_inference agent) MUST be runnable end-to-end on the compatibility backend against LM Studio. A passing integration test (`tests/integration/test_067_env_bootstrap_lmstudio.py` or equivalent — gated by `LMSTUDIO_AVAILABLE` env var so CI without the model still runs) MUST exist.
- **FR-009**: If the chosen harness is `opencode`, the adapter MUST follow the [[junie_own_harness]] precedent: mirror opencode's one-shot CLI invocation pattern rather than subclassing `OpenCodeAdapter`. Adapter-per-harness is the rule, not interface inheritance.

### Non-Functional Requirements

- **NFR-001**: First-token latency on the compatibility backend against OpenAI MUST be within 10% of first-token latency on the codex backend against OpenAI for the same prompt. This bounds the cost of "compatibility tax."
  - **CI enforcement**: out of scope for 067. NFR-001 is measured by an operator-run benchmark (`tests/integration/test_067_perf_compat_vs_codex.py`, gated by `OPENAI_API_KEY` presence). A regression is judged by hand against a checked-in `baseline-codex.json` fixture. A CI lane will be added in a follow-up spec once either (a) a self-hosted LM Studio runner exists or (b) per-PR OpenAI API spend is approved.
- **NFR-002**: The compatibility backend's request body MUST be inspectable in the daemon log at `log_level: debug` without further instrumentation. (The 065 debugging cycle wasted time guessing at payloads.)

### Success Criteria

- **SC-001**: A fresh-clone operator following `quickstart.md` for the compatibility backend lands a single website card to `IN_REVIEW` against LM Studio in under 60 minutes of wall-clock time, with no edits to source code.
- **SC-002**: Zero `unsupported tool type` or `developer role rewritten` warnings appear in the LM Studio console during a full 065-style fix cycle on the compatibility backend.
- **SC-003**: Backend abstraction passes a "swap test": flipping `backend: codex ↔ <compat>` in `config.yaml` and restarting the daemon requires no other configuration change and no code change.

## Out of Scope

- Removing or deprecating the codex backend. It remains the recommended path for OpenAI-targeted deployments.
- Implementing a model router that picks backend per role. (Future spec; flagged in 065 next_roadmap.)
- Self-hosting a hosted-tool equivalent for web_search (e.g. SearXNG, Brave Search API). The compatibility backend executes web_search agent-side using whichever client the operator configures; the choice of client is operator-controlled, not a coordinare concern.
- Cross-backend session migration. A card started on codex stays on codex; one started on compat stays on compat. Mid-card backend hot-swap is not supported.
- Modifying the coordinare's LangGraph topology. This spec changes only the performer adapter layer.

## Files Likely to Change

- `agent/performer/src/performer/backends/` — new `compat.py` (or `opencode.py`) adapter alongside `codex.py` / `junie.py`.
- `agent/performer/src/performer/main.py` — backend selection wiring.
- `agent/performer/src/performer/protocol.py` — possibly tighten the adapter Protocol to make "agent-side tools only" a type-level guarantee.
- `src/coordinare/services/http_performer_service.py` — surface upstream HTTP status/body in the result envelope.
- `src/coordinare/graph/nodes/handle_system_error.py` — classify upstream-status-based transients consistently with `github_retry`.
- `config.example.yaml` — document the new backend value with a worked example.
- `packages/service_inference/src/coordinare_service_inference/prompt.py` — review tool list for any web_search reference that assumed hosted execution.
- `tests/integration/test_067_*.py` — new integration tests for the LM Studio happy path and the transparent-error path.
- `quickstart.md` (or `docs/quickstart-selfhosted.md`) — operator-facing setup against LM Studio.

## Open Questions

All resolved during planning — see `research.md` for the decisions.

1. ~~**Harness choice**~~ — **Resolved (R1)**: opencode CLI selected; see `research.md` §R1.
2. ~~**Backend value name**~~ — **Resolved (R2)**: `opencode_compat` registered in `performer.backends.supported_backends`.
3. ~~**web_search agent-side implementation**~~ — **Resolved (R3)**: deferred to harness; spec only requires the request stay LCD-shaped. See `research.md` §R3.
4. ~~**`developer` → `system` remap**~~ — **Resolved (R4)**: remap with one-time WARN, gated on `compat_remap_developer_role`. See `research.md` §R4 and `_remap_developer_role` in `agent/performer/src/performer/backends/opencode_compat.py`.
5. ~~**Debug logging of request bodies**~~ — **Resolved (R5)**: `_redact_request_body` covers denylisted keys and secret-value patterns before logging; outbound body never mutated. See `research.md` §R5 and `tests/unit/test_067_request_body_redaction.py`.

## Related

- 065 self-hosted validation gap (resolved-but-blocked-on-this): `feedback_self_hosted_models.md` in memory; the [[065-qa-cycle]] live-validation log.
- [[feedback_interface_first_design]] — backend adapter Protocol is the right interface to harden here, not adapter inheritance.
- [[feedback_junie_own_harness]] — precedent: each harness gets its own adapter, mirroring its own CLI conventions; do not subclass.
- 066 (`066-unify-card-pickup`) — orthogonal; unifies the coordinare-side pickup path. 067 is the performer-side compatibility story. The two should not be sequenced — they touch different layers.
