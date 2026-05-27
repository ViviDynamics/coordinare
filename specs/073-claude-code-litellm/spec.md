# Feature Specification: Claude Code Backend via LiteLLM Proxy

**Feature Branch**: `073-claude-code-litellm`
**Created**: 2026-05-24
**Status**: Draft
**Input**: User description: "Route the existing claude_code performer backend through a LiteLLM proxy as the LLM provider. The claude CLI subprocess already inherits the performer process env, so the LiteLLM integration is primarily a config + secret-plumbing + docs effort: expose proxy base URL and auth token as first-class performer config (env-overridable), thread them into the subprocess env that claude_code.py spawns, and document how operators bring up a LiteLLM proxy and which non-Anthropic models are validated against the claude CLI. Out of scope: replacing the claude CLI with the LiteLLM SDK directly; modifying performer backends other than claude_code; building or hosting the LiteLLM proxy itself."

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Operator points the claude_code backend at a LiteLLM proxy (Priority: P1)

A coordinare operator wants to run cards through the existing `claude_code` performer backend but route every LLM call to a LiteLLM proxy they already operate. They set two config values (proxy URL and proxy auth token) on the performer; nothing else about how cards are dispatched, claimed, or completed changes.

**Why this priority**: This is the entire feature. Without it the operator cannot redirect Claude Code traffic away from Anthropic's API without forking the adapter.

**Independent Test**: Set the proxy URL to a LiteLLM instance configured to log requests; dispatch one card; verify the proxy receives the request and the claude CLI completes the turn successfully.

**Acceptance Scenarios**:

1. **Given** a running LiteLLM proxy and a performer configured with the proxy URL and auth token, **When** a card is dispatched to the `claude_code` backend, **Then** the claude CLI's outbound LLM calls hit the proxy (not `api.anthropic.com`) and the turn completes normally.
2. **Given** the proxy URL is unset (or set to empty), **When** a card is dispatched to the `claude_code` backend, **Then** the claude CLI defaults to its built-in endpoint exactly as it does today — no regression for operators not using LiteLLM.
3. **Given** the proxy URL is set but the auth token is missing or wrong, **When** a card is dispatched, **Then** the claude CLI surfaces the proxy's auth-failure response and the performer reports the turn as failed without crashing or hanging.

---

### User Story 4 — In-container response shim normalizes proxy output for the claude CLI (Priority: P1)

The claude CLI's response parser is strict about Anthropic-canonical SSE / JSON shape. LiteLLM-translated responses from non-Anthropic providers are spec-compliant-ish but not bit-for-bit canonical (e.g. `signature: null` on `thinking` blocks, `thinking`+empty-`text` content arrays). When such a response reaches the CLI, it logs `Error streaming, falling back to non-streaming mode: Content block not found` and produces zero output to the operator — observed in `2.1.148` against `litellm.vividynamics.com` for `spark/qwen3.6:35b`.

The performer container therefore runs a localhost reverse-proxy shim between the claude CLI and the operator's LiteLLM proxy. The shim forwards requests upstream and normalizes responses to whatever shape the claude CLI's parser accepts — dropping or collapsing content blocks the CLI rejects — without altering the operator's LiteLLM server.

**Why this priority**: Without the shim, US1's env-injection contract is technically correct but functionally broken for every non-Anthropic model the proxy might route to (which is the *point* of US2). The shim is the surface that makes US1 actually do what its acceptance scenarios claim.

**Independent Test**: Dispatch one card through the `claude_code` backend with `LITELLM_PROXY_BASE_URL` pointed at a proxy whose downstream model emits `thinking`-bearing responses (e.g. `spark/qwen3.6:35b`); the CLI receives a parser-acceptable response via the shim and the turn completes normally.

**Acceptance Scenarios**:

1. **Given** the shim is running and the upstream LiteLLM proxy returns an Anthropic-shape response containing a `thinking` block with `signature: null`, **When** the claude CLI fetches via the shim, **Then** the `thinking` block is removed from the response the CLI sees and the turn completes (matches FR-009).
2. **Given** the upstream returns a streaming response whose SSE event sequence the claude CLI rejects, **When** the shim relays it, **Then** the shim rewrites the SSE event stream to one the CLI accepts; the operator sees the turn complete via the same flow as direct Anthropic.
3. **Given** the shim is running, **When** the operator inspects performer logs at INFO level, **Then** they see one log line per upstream request (path, status, latency) but never the bearer token, request body, or upstream response body.
4. **Given** the shim cannot start (port bind failure, etc.), **When** the backend tries to launch the claude CLI, **Then** the backend reports a startup error via the existing failure path and does not silently fall back to direct-Anthropic.

---

### User Story 2 — Operator runs a non-Anthropic model through the same path (Priority: P2)

A coordinare operator configures their LiteLLM proxy to route the model name the claude CLI sends (e.g. `claude-opus-4-7`) to a non-Anthropic provider (e.g. an OpenAI-hosted model). They want to know which of those mappings have been validated to actually complete real cards, and which haven't.

**Why this priority**: The proxy works regardless of which downstream model is wired up — but operators need a published compatibility statement, not trial-and-error, before betting on a given model for production cards.

**Independent Test**: Follow the documented setup for one validated non-Anthropic model, dispatch a card, confirm the card reaches completion (or blocked-with-questions) without performer-level errors.

**Acceptance Scenarios**:

1. **Given** the published documentation, **When** an operator reads the "Validated models" section, **Then** they can identify at least one non-Anthropic model that has been confirmed to complete a representative card end-to-end.
2. **Given** an operator follows the documented LiteLLM proxy configuration, **When** they dispatch a card, **Then** the card progresses through the same states (claimed → working → submitted) as it would with the default Anthropic endpoint.

---

### User Story 3 — Operator brings up a LiteLLM proxy from the docs (Priority: P3)

A new operator who has never run LiteLLM follows this feature's documentation to bring up a minimal proxy that the `claude_code` backend can talk to.

**Why this priority**: Without setup docs the feature is technically functional but unusable by anyone who isn't already running LiteLLM. P3 because the primary audience (existing operators with their own proxy) is served by P1 alone.

**Independent Test**: A reader following only this spec's docs (no other LiteLLM knowledge) can stand up a local proxy and successfully route one card through it.

**Acceptance Scenarios**:

1. **Given** the published quickstart, **When** an operator runs the documented commands on a fresh machine, **Then** they end up with a running LiteLLM proxy that the `claude_code` backend can use without code changes.

---

### User Story 5 — Long-running jobs don't fail on GitHub App token expiry (Priority: P1 / bug)

The coordinare mints fresh installation tokens (1-hour TTL) and forwards them on every status poll. The stdio performer path applies these refreshed tokens to the in-flight `Score` and `Stand`; the HTTP performer path does not — `HttpPerformerService.check_status` accepts the fresh-token payload from `monitor_performer.py` but never forwards it to the container, and `_perform_job`'s internal status loop has no input channel for refreshed credentials. When the implementation phase crosses the 1-hour boundary, `push_branch` fails with HTTP 401 from `api.github.com`, the workspace setup raises `WorkspaceSetupError`, and the card is moved to BLOCKED for a transient credential expiry — observed on website issue #70.

**Why this priority**: Any card whose implementation runs longer than ~55 minutes (the coordinare's refresh buffer) is silently affected. The probability of a token-expiry-induced false-blocked grows with model speed and prompt complexity.

**Independent Test**: Mock a long-running HTTP performer job; mock the coordinare to send a rotated `github_token` in the status payload one cycle in; assert the performer's `perf.score.github_token` and `perf.stand.git_env` reflect the new token before the next `push_branch`.

**Acceptance Scenarios**:

1. **Given** a card is dispatched to an HTTP performer and runs past the 1-hour App-token TTL, **When** the coordinare's `monitor_performer.py` includes a freshly-minted `github_token` in the status `payload`, **Then** the performer container's in-flight `Score.github_token` and `Stand.git_env` are updated before the next git operation, and `push_branch` succeeds.
2. **Given** the coordinare sends NO `github_token` in the status payload (legacy/stdio path or token still valid), **When** check_status runs, **Then** the performer's existing token is left untouched (no regression).
3. **Given** the performer's HTTP server receives a refreshed-secret update mid-job, **When** the secret is applied to in-flight job state, **Then** the refreshed token never appears in stdout/stderr, structured logs, or the job's `result.summary` (FR-004-aligned).

---

### User Story 6 — Cycle re-entry does not re-block a card that already has an open PR (Priority: P1 / bug)

When a card with an open PR transiently fails (e.g. workspace setup error from a 401 push), the pickup path currently re-runs `assess_card`, which re-reads all issue comments — including the original clarification request that has already been answered by the existence of the PR — and re-blocks the card on stale questions. Observed on website issue #70: a token-expiry push failure surfaced as `WorkspaceSetupError`, the card returned to TODO, the next cycle re-assessed it, and the assessor's first comment from the original clarification round was treated as an unanswered open question.

**Why this priority**: This couples a transient credential bug (US5) to a permanent re-block. Even after US5 lands, any future transient pickup failure on a PR-bearing card produces the same false-positive.

**Independent Test**: Construct a card that has `pr_url` set (open PR) and a stale `assess`-phase question in its history; drive one pickup cycle; assert assess_card is short-circuited (or skipped) and the card routes to the post-PR phase corresponding to its current PR state.

**Acceptance Scenarios**:

1. **Given** a card has a non-empty `pr_url` and a discoverable open PR for the card's branch, **When** the coordinare enters pickup, **Then** assess_card / classify_card are short-circuited and the card routes to the PR-monitoring phase (or wherever the open-PR state machine dictates) without re-reading historical clarification comments.
2. **Given** a card has no `pr_url` and no recoverable open PR, **When** pickup runs, **Then** assess_card runs normally (no regression for first-pass cards).
3. **Given** a card's `pr_url` points to a closed/merged PR, **When** pickup runs, **Then** existing closed-PR handling (e.g. `count_closed_prs_for_issue` ceiling in dispatch_performer.py:171) governs behavior — this story does not change closed-PR logic.

---

### Edge Cases

- **Proxy reachable but slow**: The claude CLI's own timeouts govern this; the performer does not introduce a new timeout. If the CLI times out, the turn fails by the existing failure path.
- **Auth token in logs**: The proxy auth token must never appear in performer logs, CI artifacts, or card state. Treated the same as `ANTHROPIC_API_KEY` today.
- **Mixed deployments**: One performer instance uses LiteLLM, another uses Anthropic direct, both writing to the same coordinare — supported, since the routing decision is per-performer-process env.
- **Proxy returns a malformed response**: The claude CLI surfaces this as a turn failure; the performer reports it as such with no special handling required by this feature.
- **Config set but `claude_code` not the active backend**: Setting LiteLLM config when the active backend is `opencode`/`hermes`/`junie` has no effect and emits no error — it is a `claude_code`-only knob.
- **Upstream emits `thinking` blocks with `signature: null`**: The shim strips them before the claude CLI sees them (FR-009). Operators lose visibility into model reasoning in performer logs but the turn completes; the shim's DEBUG log is the diagnostic surface.
- **Shim port-bind failure**: Treated as a backend startup error (FR-010). The performer reports the failure via the existing path; no silent fallback to `api.anthropic.com`.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The performer MUST expose two new configuration values, "LiteLLM proxy base URL" and "LiteLLM proxy auth token", that are settable via the same config mechanism as other performer settings (config file plus env override).
- **FR-002**: When both LiteLLM proxy config values are set and the active backend is `claude_code`, the performer MUST pass them to the claude CLI subprocess as the environment variables the CLI already recognizes for endpoint redirection and authentication, so no claude-CLI command-line flag changes are required.
- **FR-003**: When the LiteLLM proxy base URL is unset (empty or missing), the performer MUST NOT inject any related environment variable into the subprocess, preserving today's default behavior (claude CLI talks to its built-in endpoint).
- **FR-004**: The performer MUST treat the LiteLLM proxy auth token as a secret: it MUST NOT appear in performer stdout/stderr, structured logs, persisted state, or any card-state field. Existing secret-handling conventions for `ANTHROPIC_API_KEY` apply.
- **FR-005**: The LiteLLM proxy config MUST be ignored when the active backend is anything other than `claude_code`. Setting it for an `opencode`/`hermes`/`junie`/etc. backend MUST NOT change that backend's behavior and MUST NOT produce a startup error.
- **FR-006**: The project MUST publish operator documentation that (a) explains how to bring up a minimal LiteLLM proxy suitable for the `claude_code` backend and (b) lists which non-Anthropic models have been validated end-to-end through this path.
- **FR-007**: A turn that fails because the proxy is unreachable, rejects auth, or returns a malformed response MUST surface as a normal performer-side turn failure (no new failure mode, no crash, no infinite hang beyond the existing claude-CLI timeout).
- **FR-008**: When the proxy URL is set and `claude_code` is the active backend, the performer MUST start an in-container response-translation shim before launching the claude CLI subprocess. The shim MUST bind only to a loopback interface (`127.0.0.1`) on an ephemeral port and MUST forward requests to the operator-configured LiteLLM proxy using the operator-supplied bearer token. The `ANTHROPIC_BASE_URL` injected into the subprocess env (per FR-002) MUST point at the shim's loopback URL — never directly at the upstream proxy.
- **FR-009**: The shim MUST normalize upstream responses to a shape the claude CLI's parser accepts. Specifically: `thinking` content blocks (including those with `signature: null`) MUST be removed from both non-streaming JSON responses and the SSE event stream (`content_block_start` / `content_block_delta` / `content_block_stop` events whose block is of type `thinking`). The shim MUST log the removal at DEBUG level inside the shim layer; the DEBUG log MAY include block type and byte length but MUST NOT include the stripped reasoning text (which remains an upstream response body under FR-011). Requests to paths other than `POST /v1/messages` MUST be forwarded to the upstream proxy unmodified (with bearer auth applied) — only `POST /v1/messages` responses are subject to normalization.
- **FR-010**: The shim's lifecycle MUST be bound to the `claude_code` backend's `start()` / `stop()`. If the shim cannot bind its port or otherwise fails to come up, the backend MUST surface the failure through the existing startup-error path and MUST NOT fall back to direct-Anthropic routing — i.e., it fails closed, never silently bypassing the operator's proxy. Concretely: the backend raises a `BackendStartupError` (or sets `status="error"` on the existing startup result type) before any subprocess is spawned, identical in shape to today's surface for missing-credentials failures.
- **FR-012** (US5): The HTTP performer path MUST propagate a refreshed GitHub token from the coordinare's status payload through to the in-flight job's `Score.github_token` and `Stand.git_env`, identically to how the stdio path handles it at `agent/performer/src/performer/main.py:2454-2460`. The refresh MUST happen before the next git operation runs in the job, with no externally observable change to `result.summary` or logs.
- **FR-013** (US6): When a card's recorded state (or a fresh `find_pr_for_issue` lookup) indicates an open PR exists for the card's branch, pickup MUST NOT re-execute `assess_card` against the issue's comment history. It MUST instead route to the appropriate PR-bearing phase. Closed-PR handling is unchanged.
- **FR-011**: The shim MUST inherit the secret-hygiene rules of FR-004. The bearer token, request bodies, and upstream response bodies MUST NOT appear in performer stdout/stderr, structured logs, CI artifacts, or persisted card state. The shim MAY emit one INFO log line per upstream request limited to method, path, upstream status code, and latency.

### Key Entities

- **LiteLLM proxy URL**: Operator-supplied HTTP(S) endpoint that the claude CLI is redirected to. One value per performer process.
- **LiteLLM proxy auth token**: Operator-supplied bearer credential the proxy expects. One value per performer process. Treated as a secret.
- **Validated model matrix**: A documented list of (proxy-routed model name → downstream provider) pairs that have been confirmed to complete a representative card end-to-end. Lives in the operator docs, not in code.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator with a running LiteLLM proxy can route the `claude_code` backend through it by changing only configuration — zero code changes, zero fork of the adapter.
- **SC-002**: With LiteLLM proxy config unset, performers exhibit byte-identical subprocess-environment behavior to the pre-feature baseline (regression-tested by diffing the env dict the subprocess sees).
- **SC-003**: At least one non-Anthropic model is listed in the published Validated Model Matrix with a recorded date of validation and the card scenario it was validated against.
- **SC-004**: The LiteLLM proxy auth token appears in zero performer log lines, zero CI artifacts, and zero persisted card-state fields across a full end-to-end run.
- **SC-005**: A new operator following only the published quickstart can stand up a LiteLLM proxy and route one card through it in under 30 minutes.
- **SC-007** (US5): A simulated HTTP performer job that runs past the App-token TTL and receives a refreshed token via the coordinare status payload completes `push_branch` successfully on the first attempt (no 401, no force-push fallback triggered by auth). Verified by a unit test that mocks the rotated token and asserts the refreshed value reaches `perf.score.github_token` before the next `push_branch` call.
- **SC-008** (US6): A card with `pr_url` set is not re-blocked by historical clarification questions on cycle re-entry. Verified by a unit test that constructs a card with a pre-set `pr_url` plus stale `open_questions` history and asserts assess_card is skipped (or its state contribution is dropped) on the second pickup.
- **SC-006**: With the shim active and `LITELLM_PROXY_BASE_URL` pointed at a proxy whose downstream model emits `thinking`-bearing responses (e.g. `spark/qwen3.6:35b`), a representative card completes end-to-end through the `claude_code` backend with zero `Content block not found` errors in the claude CLI's stderr across 5 consecutive turns.

## Assumptions

- Operators already have (or are willing to stand up) their own LiteLLM proxy. This feature does not host, package, or operate a proxy on the operator's behalf.
- The claude CLI's existing environment-variable contract for endpoint redirection and bearer auth is stable; if it changes upstream, this feature's plumbing changes accordingly but the spec does not.
- The proxy is responsible for any model-name translation between what the claude CLI sends and what the downstream provider expects. The performer does not rewrite model names.
- Operator docs live in the existing project docs surface (no new docs site).

## Out of Scope

- Replacing the claude CLI subprocess with direct calls to the LiteLLM Python SDK.
- Modifying performer backends other than `claude_code` (e.g. `opencode`, `hermes`, `junie`).
- Building, packaging, hosting, or operating the LiteLLM proxy itself.
- Per-card or per-backend model routing logic inside the performer (model selection remains the proxy's job).
- Cost accounting, rate limiting, or usage telemetry on top of LiteLLM (the proxy owns these).
- Modifying the claude CLI's own response parser. Normalization happens in the in-container shim (FR-008..FR-011), not by patching the upstream CLI.
- Persisting or relaying the stripped `thinking` content to operators outside the shim's DEBUG log.
