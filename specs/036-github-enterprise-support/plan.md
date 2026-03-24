# Implementation Plan: GitHub Enterprise Support

**Branch**: `036-github-enterprise-support` | **Date**: 2026-03-24 | **Spec**: [spec.md](./spec.md)

## Summary

Make the GitHub API base URL and GraphQL endpoint configurable in `config.yaml` so coordinare and performer can target GitHub Enterprise Server instances. Two new fields on `ProjectConfiguration` flow through to `GitHubService`, `GitHubAppAuth`, and the performer dispatch payload. No new dependencies required.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: pydantic-settings, httpx, gql (all existing) — **no new dependencies**
**Storage**: Existing `config.yaml` — extended with `github_api_url` and `github_graphql_url`
**Constraints**: Must default to `https://api.github.com` for backward compatibility
**Scale/Scope**: 4 modified files, ~10 new unit tests

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | Eliminates hardcoded URLs in favor of config-driven values |
| II. Testing Discipline | PASS | Each modified file gets corresponding test coverage |
| III. User Experience | PASS | Zero-config for existing users; single key for GHES |
| IV. Performance by Design | PASS | URL resolution at startup only |
| V. Clarity Before Action | PASS | No open clarifications |

## Source Code (files changed)

```text
src/coordinare/config.py                          # MODIFIED — add github_api_url, github_graphql_url
src/coordinare/services/github.py                 # MODIFIED — wire endpoint from config
src/coordinare/auth/app.py                        # MODIFIED — derive token URL from github_api_url
src/coordinare/__main__.py                        # MODIFIED — pass URLs to constructors
agent/performer/src/performer/github.py          # MODIFIED — replace _GITHUB_API with env-var override
agent/performer/src/performer/config.py          # MODIFIED — add GITHUB_API_URL setting
tests/unit/test_config.py                        # MODIFIED — URL validation tests
tests/unit/services/test_github_service.py       # MODIFIED — custom endpoint tests
```

## Detailed Implementation Plan

### Step 1 — Extend Config Model (`src/coordinare/config.py`)

Add `github_api_url: str = Field(default="https://api.github.com")` and `github_graphql_url: str = Field(default="https://api.github.com/graphql")` to `ProjectConfiguration`. Add a field validator that strips trailing slashes and validates `http`/`https` scheme.

### Step 2 — Wire URLs into GitHubService

In `__main__.py`, pass `config.github_graphql_url` as `endpoint` to `GitHubService(...)`. In `services/github.py`, change the `endpoint` default from hardcoded URL to `None` and require callers to supply it.

### Step 3 — Update GitHubAppAuth (`src/coordinare/auth/app.py`)

Accept `api_url` as a constructor parameter. Replace the hardcoded token exchange URL `"https://api.github.com/app/installations/..."` with `f"{self._api_url}/app/installations/..."`. Wire from `config.github_api_url` in `__main__.py`.

### Step 4 — Update Performer GitHub Client

Add `GITHUB_API_URL: str = "https://api.github.com"` to performer's `Settings`. Replace `_GITHUB_API` constant with `settings.GITHUB_API_URL`. Add `GITHUB_API_URL` to the dispatch environment so the performer connects to the same GHES instance.

## Complexity Tracking

| Change | Scope | Justification |
|--------|-------|---------------|
| 2 new config fields | ~15 LOC | Minimal addition to existing config model |
| 4 modified source files | ~40 LOC | Replacing hardcoded URLs with config-driven values |
| Performer-side change | ~10 LOC | Performer must connect to same GHES instance |
