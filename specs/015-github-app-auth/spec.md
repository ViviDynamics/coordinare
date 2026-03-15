# Feature Specification: GitHub Auth Modes & Webhook Support

**Feature Branch**: `015-github-app-auth`
**Created**: 2026-03-12
**Status**: Draft
**Input**: Support PAT and GitHub App auth modes with configurable polling and optional webhook endpoint

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Run Coordinare as a GitHub App (Priority: P1)

An operator wants Coordinare to act as a dedicated bot identity (`coordinare[bot]`) rather than impersonating their personal GitHub account. They register a GitHub App, generate a private key, and point Coordinare at it. Coordinare automatically generates short-lived tokens, refreshes them before expiry, and all GitHub interactions (board moves, comments, PR creation) appear under the app's identity.

**Why this priority**: This is the core capability of the feature. Without it there is no GitHub App support. Running as a bot identity is the primary motivation for the whole spec.

**Independent Test**: Configure Coordinare with `github.auth: app` and valid app credentials. Start it against a test board. Verify board moves and comments appear as `[bot]` in the GitHub UI and that the process runs without credential errors for longer than one hour (token refresh exercised).

**Acceptance Scenarios**:

1. **Given** `github.auth: app` with valid `app_id`, `private_key_path`, and `installation_id`, **When** Coordinare starts, **Then** it successfully obtains an installation access token and begins operating normally.
2. **Given** a running Coordinare in app mode, **When** the installation token is within 5 minutes of expiry, **Then** Coordinare transparently refreshes it without interrupting ongoing operations.
3. **Given** `github.auth: app` with an invalid private key, **When** Coordinare starts, **Then** it fails fast with a clear error message identifying the misconfiguration.
4. **Given** a token refresh fails transiently, **When** the next GitHub API call is made, **Then** Coordinare retries token acquisition before surfacing an error.

---

### User Story 2 — Continue Running as a PAT User (Priority: P1)

An operator who already has Coordinare configured with a personal access token should not need to change anything. PAT mode remains the default. Existing config files continue to work without modification.

**Why this priority**: Backwards compatibility. All existing deployments use PAT mode and must not break.

**Independent Test**: Start Coordinare with an existing config containing only `github.token`. Verify it operates identically to before this feature was introduced.

**Acceptance Scenarios**:

1. **Given** a config with `github.auth: pat` (or no `auth` field, defaulting to `pat`), **When** Coordinare starts, **Then** it uses the static token for all GitHub API calls without any refresh logic.
2. **Given** a config with only `github.token` set and no `auth` field, **When** Coordinare starts, **Then** it defaults to PAT mode without error.
3. **Given** `github.auth: pat` with no `github.token` set, **When** Coordinare starts, **Then** it fails with a clear error identifying the missing token.

---

### User Story 3 — Receive GitHub Webhook Events (Priority: P2)

An operator running Coordinare on a machine reachable from the internet (VPS, cloud VM, home server with port forwarding, or a tunnel like `gh webhook forward`) wants to reduce polling latency. They configure a webhook secret and endpoint path, register the webhook in their GitHub App or repository settings, and Coordinare processes events immediately as they arrive rather than waiting for the next poll cycle.

**Why this priority**: Optional enhancement. Coordinare works correctly without it; webhooks reduce latency for operators who can receive them.

**Independent Test**: Enable `webhooks.enabled: true`, configure the secret, and send a test webhook payload via `gh webhook forward` or `curl`. Verify the graph cycle fires immediately and the event is acknowledged with HTTP 200.

**Acceptance Scenarios**:

1. **Given** `webhooks.enabled: true` with a valid `secret`, **When** a GitHub webhook POST arrives with a valid HMAC-SHA256 signature, **Then** Coordinare acknowledges it with HTTP 200 and triggers a graph cycle.
2. **Given** a webhook POST with an invalid or missing signature, **When** Coordinare receives it, **Then** it returns HTTP 401 and does not trigger a graph cycle.
3. **Given** `webhooks.enabled: false` (default), **When** Coordinare starts, **Then** no webhook endpoint is registered.
4. **Given** webhooks are enabled and a cycle is already running, **When** a webhook event arrives, **Then** the event is queued and triggers a cycle after the current one completes (no concurrent cycles).

---

### User Story 4 — Configure or Disable Polling (Priority: P2)

An operator wants to tune how frequently Coordinare polls GitHub, or disable polling entirely when they are confident webhook delivery is reliable.

**Why this priority**: Operational flexibility. Operators on low-traffic boards may want slower polling; operators relying solely on webhooks want no polling overhead.

**Independent Test**: Set `polling.interval_seconds: 0`. Verify Coordinare starts, logs that polling is disabled, and only processes events when a webhook fires. Set `polling.interval_seconds: 120` and verify cycles fire at the configured interval.

**Acceptance Scenarios**:

1. **Given** `polling.interval_seconds: 30` (default), **When** Coordinare runs, **Then** it polls the board approximately every 30 seconds.
2. **Given** `polling.interval_seconds: 0`, **When** Coordinare runs, **Then** it logs "polling disabled" at startup and schedules no polling cycles.
3. **Given** `polling.interval_seconds: 0` and `webhooks.enabled: false`, **When** Coordinare starts, **Then** it logs a warning that no trigger source is active, but starts successfully.

---

### Edge Cases

- What happens when the GitHub App private key file does not exist or is not readable at startup?
- What happens if `app_id` or `installation_id` are set but `private_key_path` is missing?
- What happens when a webhook event arrives while `webhooks.enabled: false` (stale registration on GitHub's side)?
- What happens if the board poll and a webhook event fire simultaneously?
- What happens when the installation token request succeeds but returns a malformed response?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST support two auth modes — `pat` and `app` — selectable via `github.auth`; default MUST be `pat`.
- **FR-002**: In PAT mode, system MUST use the static value of `github.token` for all GitHub API calls with no refresh logic.
- **FR-003**: In App mode, system MUST generate a signed JWT from `github.app_id` and the RSA private key at `github.private_key_path`, exchange it for an installation access token scoped to `github.installation_id`, and cache the token until it is within 5 minutes of expiry.
- **FR-004**: In App mode, system MUST refresh the installation token automatically before expiry so no GitHub API call ever uses an expired credential.
- **FR-005**: Both auth modes MUST present the same interface to `GitHubService` — a callable that returns a current token string — so no other part of the system needs to know which mode is active.
- **FR-006**: The token produced in App mode MUST work for performer git operations (clone, push) via the existing `x-access-token` credential mechanism.
- **FR-007**: System MUST fail at startup with a descriptive, actionable error if required config fields for the selected auth mode are absent or invalid.
- **FR-008**: System MUST support `polling.interval_seconds` (default: 30); a value of `0` MUST disable all polling cycles.
- **FR-009**: System MUST support a `webhooks` config section with `enabled` (default: `false`), `secret` (HMAC-SHA256 key), and `path` (default: `/webhook/github`).
- **FR-010**: When `webhooks.enabled: true`, system MUST register a webhook endpoint on the existing HTTP server, validate `X-Hub-Signature-256` on every request, reject invalid or missing signatures with HTTP 401, and trigger a graph cycle on valid events.
- **FR-011**: Webhook-triggered cycles MUST NOT run concurrently with a polling cycle; if a cycle is in progress when a webhook fires, the webhook queues the next cycle.
- **FR-012**: When `webhooks.enabled: false`, no webhook endpoint MUST be registered.
- **FR-013**: System MUST log a warning at startup when both `polling.interval_seconds: 0` and `webhooks.enabled: false`.

### Key Entities

- **GitHubAuth** *(protocol)*: Exposes a single operation — retrieve a current, valid token string. Implementations hide all credential lifecycle details from callers.
- **PatAuth**: Holds a static token string; satisfies the GitHubAuth protocol with no refresh logic.
- **AppAuth**: Holds app credentials; satisfies GitHubAuth by managing JWT generation, installation token acquisition, and automatic refresh.
- **PollingConfig**: `interval_seconds` — controls polling cadence; `0` means disabled.
- **WebhookConfig**: `enabled`, `secret`, `path` — controls whether and how the webhook endpoint operates.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can switch from PAT to GitHub App mode by changing config fields only — zero code changes required.
- **SC-002**: Coordinare in App mode runs for 24+ hours without a manual token refresh or any credential-related error.
- **SC-003**: All existing PAT-based configs continue to work after upgrading with no changes required.
- **SC-004**: A valid webhook event received while polling is disabled triggers a graph cycle within 2 seconds of receipt.
- **SC-005**: A webhook POST with an invalid signature is rejected with HTTP 401 and the rejection is recorded in structured logs.
- **SC-006**: Setting `polling.interval_seconds: 0` results in zero scheduled polling cycles; verified by log inspection over a 5-minute window.
- **SC-007**: Startup validation catches all missing required auth fields and surfaces actionable error messages before the main loop begins.

## Assumptions

- The existing dashboard HTTP server (FastAPI) is reused as the webhook listener; no second HTTP server is introduced.
- GitHub App private key is stored as a PEM file on disk; inline key string in config is a stretch goal, not required.
- Only installation-scoped tokens are needed; OAuth user tokens are out of scope.
- Webhook event filtering (reacting to specific event types only) is out of scope; any valid signed POST triggers a cycle.
- Rate limiting and deduplication of rapid webhook bursts are out of scope.
- The performer always receives a plain token string and does not need to know whether it originated from a PAT or an App installation.
