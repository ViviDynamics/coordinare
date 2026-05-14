# Feature Specification: QA Cycle 062

**Feature Branch**: `062-qa-cycle`
**Created**: 2026-05-11
**Status**: Draft — actively collecting QA findings
**Input**: User description: "Another QA and optimization iteration on coordinare, started during live testing of the `website` symphony after the 060/061 env-cache work landed. This spec bundles small, independent fixes surfaced by operator interaction with the running daemon. More findings will be appended as testing continues."

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Force-Bootstrap Surfaces Real Performer Registration Issues (Priority: P1)

An operator clicks **Force bootstrap now** on a symphony's dashboard detail page. The button hits `POST /api/symphonies/{name}/env-bootstrap`, which currently returns `202 Accepted` as long as the `env_cache_service` handle is present on `daemon.state` — even when the configured `env_bootstrap_performer_id` points at a performer that isn't registered with the daemon (typo in `config.yaml`, half-initialised startup, performer pruned by a partial reload, etc.). The bootstrap is then silently dropped on the next cycle: `_execute_bootstrap_dispatch` logs `env_cache.bootstrap_svc_not_found` and flips `on_bootstrap_complete(success=False)`, but the operator only sees "accepted" in the UI and has no signal to investigate.

The desired behaviour: the endpoint pre-flights the performer registration synchronously and returns a `503` with a message that names the missing performer and points at the config, so the operator gets immediate, actionable feedback instead of waiting for a silent dispatch failure.

**Why this priority**: The existing failure mode is one we just hit in production diagnostics — the generic "Env cache service not available" 503 masked the real cause (a half-initialised daemon with no performer registered for the bootstrap id). A clearer pre-flight prevents a class of "I pressed the button and nothing happened" support tickets.

**Independent Test**: With a symphony that has `env_bootstrap_performer_id: codex-ephemeral` configured but no `codex-ephemeral` entry in `performer_services_by_id`, POST to `/api/symphonies/{name}/env-bootstrap`. The response must be `503` with an error string that includes the performer id and references `config.yaml`.

**Acceptance Scenarios**:

1. **Given** a symphony with a registered `env_bootstrap` performer, **When** the operator forces a bootstrap, **Then** the endpoint returns `202` and the daemon dispatches normally on the next cycle.
2. **Given** a symphony whose `env_bootstrap_performer_id` is not present in `daemon.state["performer_services_by_id"]`, **When** the operator forces a bootstrap, **Then** the endpoint returns `503` with `error` naming the missing performer and `performer_id` in the body.
3. **Given** the broader `env_cache_service` handle itself is missing (older failure mode), **When** the operator forces a bootstrap, **Then** the existing 503 path still fires — the new check is additive, not a replacement.

---

### User Story 2 — RTK Token Compression in Performer Containers (Priority: P2)

Coordinare performers spend large fractions of their context budget on raw command output — `git status`, `pytest`, `cargo`, directory listings, install logs. RTK (`rtk-ai/rtk`) is a Rust CLI that wraps these commands and compresses their output 60–90% before it reaches the LLM, via an auto-rewrite hook that intercepts Bash tool calls in the host agent (Claude Code, Codex CLI, etc.).

The desired behaviour: each performer container ships with the `rtk` binary installed, and at container start the entrypoint registers the backend-specific hook so the wrapped backend CLI sees compressed output transparently. The performer protocol itself does not change — this is purely a runtime cost optimisation invisible to coordinare.

**Why this priority**: It is additive, not load-bearing — performers keep working without it. We want to measure the actual token-savings delta on real cards before broadening rollout, so the first cut targets only the Claude Code and Codex backends (both have documented hook surfaces today) and is opt-in via an env var.

**Independent Test**: With `BACKEND=codex` and `RTK_ENABLED=1`, dispatch a card that exercises long `git` and `pytest` output. Compare token usage against an otherwise-identical container with `RTK_ENABLED=0`. Token consumption on the wrapped commands should drop substantially (target: ≥50% on the commands rtk recognises), with no change to card outcomes.

**Acceptance Scenarios**:

1. **Given** a performer image built with the new RTK install step, **When** the container starts with `BACKEND=codex RTK_ENABLED=1`, **Then** `rtk init` has been run for the codex CLI before `python -m performer` exec.
2. **Given** `RTK_ENABLED` is unset or `0`, **When** the container starts, **Then** `rtk init` is skipped and behaviour matches today's image byte-for-byte.
3. **Given** the rtk install step fails at image build time, **Then** the build fails loudly (it is bootstrap, not a runtime upgrade) so we don't ship an image silently missing the binary.
4. **Given** a backend without a supported rtk hook (junie / cursor / opencode in the first cut), **When** the container starts with `RTK_ENABLED=1`, **Then** the entrypoint logs a warning and continues without registering a hook.

---

## Requirements

### Functional Requirements

- **FR-001**: `POST /api/symphonies/{name}/env-bootstrap` MUST verify that the symphony's configured `env_bootstrap_performer_id` is a key in `daemon.state["performer_services_by_id"]` before accepting the request.
- **FR-002**: If the performer is not registered, the endpoint MUST return HTTP 503 with a body of the form `{"error": "Bootstrap performer 'X' is not registered with the daemon — check that it is defined in config.yaml and that coordinare loaded it at startup.", "performer_id": "X"}`.
- **FR-003**: The new check MUST run after the existing `env_cache_service is None` check and before the `env_cache` state check, so error precedence is: daemon half-init → performer not registered → cache state not initialised → bootstrap in flight → accepted.
- **FR-004**: A regression test in `tests/unit/test_dashboard.py` MUST exercise the missing-performer 503 path and assert both the status code and the message contents.

- **FR-005**: The performer base image MUST install a pinned version of `rtk` from an upstream release artifact. Build MUST fail if the binary cannot be installed.
- **FR-006**: `agent/performer/entrypoint.sh` MUST, when `RTK_ENABLED=1`, run the backend-appropriate `rtk init` invocation for `BACKEND in {codex, claude}` before exec'ing the performer server. For other backends it MUST log a warning and continue.
- **FR-007**: `RTK_ENABLED` MUST default to off so existing containers are unchanged unless coordinare opts a performer in via its `PerformerEndpointConfig.env`.
- **FR-008**: Image build MUST be reproducible: the rtk version installed is captured in a `version` or comment in the Dockerfile, not floated to `latest`.

### Success Criteria

- **SC-001**: An operator misconfiguring `env_bootstrap_performer_id` sees an actionable 503 from the UI within one click of pressing **Force bootstrap now**, with no need to consult daemon logs.
- **SC-002**: All existing env-bootstrap dashboard tests continue to pass with the new preflight in place.
- **SC-003**: On a representative card mix with `RTK_ENABLED=1` on the codex backend, total input tokens charged to Bash-tool results drop by ≥50% vs. an `RTK_ENABLED=0` baseline, with no regression in pass-rate.
- **SC-004**: Containers with `RTK_ENABLED=0` (the default) show no measurable startup or runtime difference vs. pre-062 images.

---

## Out of Scope

- Automatically starting an ephemeral performer container from the endpoint. Ephemeral containers are spun up on demand by the dispatch path; the endpoint only validates that the *service handle* exists in coordinare's state.
- Reviving / re-initialising a half-initialised daemon. If `env_cache_service` is `None`, restarting the daemon remains the recommended remediation.
- Broader dashboard rework or unrelated env-cache UX changes.
- RTK hook integration for `opencode`, `junie`, `cursor` backends — deferred until upstream hook surfaces are confirmed. The first cut covers `codex` and `claude` only.
- Coordinare-side token accounting changes. We measure the savings using existing performer-reported usage; no new metrics plumbing in this round.
