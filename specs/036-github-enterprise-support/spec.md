# Feature Specification: GitHub Enterprise Support

**Feature Branch**: `036-github-enterprise-support`
**Created**: 2026-03-24
**Status**: Draft

## Overview

The coordinare and performer currently hardcode `https://api.github.com` as the GitHub API base URL. This prevents operators running GitHub Enterprise Server (GHES) from using coordinare against their on-premise instances. This feature makes the GitHub API base URL and GraphQL endpoint configurable, defaulting to the public GitHub.com values so existing deployments are unaffected.

## Clarifications

### Session 2026-03-24

- Q: Should we support different API URLs per project, or a single global setting? → A: A single global setting in `config.yaml` is sufficient. Multi-instance support is out of scope.
- Q: Does GHES use the same GraphQL schema as GitHub.com? → A: Yes, GHES v3.x supports the Projects V2 GraphQL API. We assume schema compatibility; coordinare does not need to detect the GHES version.
- Q: Should the performer also use the configured API URL? → A: Yes. The performer's `_GITHUB_API` constant must be overridable via an environment variable or dispatch payload so it connects to the same GHES instance as the coordinare.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Configurable REST API Base URL (Priority: P1)

An operator sets `github_api_url: "https://github.acme.corp/api/v3"` in `config.yaml`. All REST API calls from the coordinare and performer use this base URL instead of `https://api.github.com`.

**Why this priority**: Without this, GHES users cannot use coordinare at all.

**Independent Test**: Set `github_api_url` in config, construct `GitHubService`, and verify the REST client's base URL matches the configured value.

**Acceptance Scenarios**:

1. **Given** `github_api_url` is set in config.yaml, **When** `GitHubService` is constructed, **Then** all REST API calls use the configured base URL.
2. **Given** `github_api_url` is not set, **When** `GitHubService` is constructed, **Then** it defaults to `https://api.github.com`.
3. **Given** `github_api_url` is set, **When** the performer is dispatched, **Then** the dispatch payload includes the API URL so the performer connects to the same instance.

---

### User Story 2 — Configurable GraphQL Endpoint (Priority: P2)

An operator sets `github_graphql_url: "https://github.acme.corp/api/graphql"` in `config.yaml`. The coordinare's GraphQL client (`gql` transport) uses this endpoint for all board and project queries.

**Why this priority**: GraphQL is used for board management. REST-only support (US1) covers the performer but not the coordinare's core board polling.

**Independent Test**: Set `github_graphql_url` in config, construct `GitHubService`, and verify the `AIOHTTPTransport` URL matches the configured value.

**Acceptance Scenarios**:

1. **Given** `github_graphql_url` is set in config.yaml, **When** `GitHubService` initializes its GraphQL transport, **Then** the transport URL is the configured endpoint.
2. **Given** `github_graphql_url` is not set, **When** `GitHubService` initializes, **Then** it defaults to `https://api.github.com/graphql`.
3. **Given** an invalid URL (e.g. missing scheme), **When** config is loaded, **Then** pydantic validation rejects it with a clear error message.

---

### Edge Cases

- What if the operator sets a trailing slash on the URL? Normalize it.
- What if GHES uses a self-signed TLS certificate? Out of scope for this feature; operators must configure system trust stores.
- What if the GitHub App auth token exchange URL also needs to change? Yes — `GitHubAppAuth` must derive its token URL from the configured API base.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: `ProjectConfiguration` MUST include a `github_api_url` field (default: `"https://api.github.com"`) and a `github_graphql_url` field (default: `"https://api.github.com/graphql"`).
- **FR-002**: `GitHubService.__init__` MUST accept the `endpoint` from config rather than using a hardcoded default.
- **FR-003**: The performer's GitHub client (`performer/github.py`) MUST read the API base URL from an environment variable (`GITHUB_API_URL`) or the dispatch payload, falling back to `https://api.github.com`.
- **FR-004**: `GitHubAppAuth` (`src/coordinare/auth/app.py`) MUST derive its token exchange URL from `github_api_url` instead of hardcoding `https://api.github.com`.
- **FR-005**: Both URL fields MUST be validated as well-formed URLs with an `https` scheme (or `http` for local dev).
- **FR-006**: Trailing slashes on configured URLs MUST be stripped to prevent double-slash issues in path construction.

### Key Entities

- **ProjectConfiguration.github_api_url**: The REST API base URL for the GitHub instance.
- **ProjectConfiguration.github_graphql_url**: The GraphQL endpoint URL.
- **GitHubService.endpoint**: Constructor parameter already exists; will be wired from config.
- **performer._GITHUB_API**: Module-level constant; will become configurable.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A coordinare configured with a GHES URL successfully polls the board and dispatches performers without any `api.github.com` requests.
- **SC-002**: The performer creates PRs against the configured GHES instance.
- **SC-003**: Existing deployments with no `github_api_url` set behave identically to today (zero regression).
- **SC-004**: GitHub App auth token exchange uses the configured base URL.

## Assumptions

- GHES v3.x supports the same Projects V2 GraphQL API as GitHub.com. Older GHES versions are not supported.
- The operator's GHES instance is reachable from the coordinare host. Network/firewall configuration is out of scope.
- Self-signed certificate handling is the operator's responsibility (system trust store or `REQUESTS_CA_BUNDLE`).
