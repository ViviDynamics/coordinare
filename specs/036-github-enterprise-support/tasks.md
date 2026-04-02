# Tasks: GitHub Enterprise Support

**Feature Branch**: `036-github-enterprise-support`

## Implementation Tasks

- [x] T001: Add `github_api_url` and `github_graphql_url` fields to `ProjectConfiguration` with defaults matching current behavior
- [x] T002: Add field validator to strip trailing slashes and validate http/https scheme
- [x] T003: Wire `config.github_graphql_url` as `endpoint` to `GitHubService` in `__main__.py`
- [x] T004: Update `AppAuth` to accept `api_url` parameter and derive token exchange URL from it
- [x] T005: Update `build_auth()` to pass `config.github_api_url` to `AppAuth`
- [x] T006: Add `GITHUB_API_URL` setting to performer `Settings` class
- [x] T007: Replace hardcoded `_GITHUB_API` constant in performer `github.py` with config-driven `_github_api()` function
- [x] T008: Include `github_api_url` in dispatch payload (`dispatch_performer.py`) so performer connects to same instance
- [x] T009: Write unit tests for URL validation (trailing slash, invalid scheme, env var override)
- [x] T010: Write unit tests for custom endpoint wiring in `GitHubService`
- [x] T011: Write unit tests for `AppAuth` custom API URL
- [x] T012: Write unit tests for performer `GITHUB_API_URL` setting and configurable URL in github.py
- [x] T013: Run linter and fix any issues
- [x] T014: Run coordinare test suite and fix any failures
- [x] T015: Run performer test suite and fix any failures
