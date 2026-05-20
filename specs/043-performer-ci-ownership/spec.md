# 043 — Performer CI Ownership

> **Status: SUPERSEDED by [065 US5](../065-qa-cycle/spec.md#user-story-5--performer-ci-ownership-verify-build-before-handoff-priority-p1).**
> The mechanical scope of 043 is preserved verbatim in 065 US5 (FR-017 – FR-022).
> All implementation tasks have shipped on the `065-qa-cycle` branch. Treat 065
> as the authoritative reference going forward; this document is retained for
> historical context.

## Summary

Any performer role that commits and pushes code to a PR branch is
responsible for verifying the repository's CI-equivalent checks
(lint, tests, type-checks, build) pass against its own changes
before returning a success status to the coordinare. Performers
that cannot turn the tree green must return `changes_requested`
or `blocked` instead of committing a broken state.

## Motivation

Surfaced during live testing of spec 042 on PR #94
(ViviDynamics/website, card #89 — copy code block feature):

- QA added a feature spec containing `expect(...).to eq(true)`
- RuboCop's `RSpec/BeEq` cop flagged it as a style violation
- The reviewer's persona instructs the AI to "flag linter disables"
  by reading, but doesn't instruct it to actually *run* RuboCop
- The closer's persona verifies prior-thread resolution but doesn't
  re-run CI
- The tech_writer committed 14 "docs: update documentation" commits
  on top of the broken state
- The PR landed in `monitoring_pr` for human review with a red CI
  build that nobody on the bot side ever detected

The failure mode is structural: **no performer stage currently executes
the project's CI commands against its own diff**. Each stage reads code
and reasons about quality, but none of them actually run the tools
that produce CI's verdict.

## Core Principle

> If a performer commits and pushes anything, it falls on that
> performer to ensure the repo's CI build passes before handing
> control back to the coordinare.

This applies to:

- **Implementer** — when landing the initial feature commits
- **QA** — when adding test files (the current PR #94 failure mode)
- **Tech writer** — when editing docs/wiki (less risky, but doc
  code-block snippets can still fail doctest-style checks)
- **Security** — when adding advisory patches or guard-rail code
- **Reviewer / Closer** — if they ever auto-fix style issues

## Proposed Design

### 1. Persona updates

Every code-touching performer persona gains a standard directive:

> Before committing, run the project's CI-equivalent commands —
> lint, tests, type-checks, build — against your changes. If any
> fail, fix them or return `changes_requested`/`blocked` rather
> than committing a broken state. Do NOT hand control back with
> a failing tree.

The coordinare's persona_service already loads these from YAML, so
this is a one-line insert per role.

### 2. Auto-detect the CI command

Add a helper that inspects the repo for conventional CI entry points
and returns the command to run:

| File present | Command |
|---|---|
| `Gemfile` + `Rakefile` | `bundle exec rake test` |
| `Gemfile` + `.rubocop.yml` | `bundle exec rubocop` (lint) + `bundle exec rspec` (test) |
| `package.json` with `test` script | `npm test` |
| `pyproject.toml` + pytest | `pytest` (plus `ruff check` if ruff in deps) |
| `.github/workflows/ci.yml` | Parse and emulate locally (deferred — complex YAML parsing, convention detection covers 90%+ of repos) |
| `Makefile` with `ci` target | `make ci` |

Expose this detection as a tool the performer can call before
committing.

### 3. Coordinare-side gate (defence in depth)

When `_advance_stage` transitions the final stage to `monitoring_pr`,
run the detected CI command against the PR branch. If it fails, route
the card back to the implementer with the failure output in
`relay_feedback`. This catches the case where a persona forgets to
check.

### 4. Reviewer / Closer personas get explicit lint instruction

Both reviewer personas add:

> Run the project's linter (`rubocop`, `eslint`, `ruff`, etc.) against
> the PR's changed files as part of your review. Include any offenses
> in your comments. Do not approve if lint fails.

## Files to Change

| File | Change |
|---|---|
| `src/coordinare/services/persona_service.py` | Add CI-ownership directive to implementer, qa, tech_writer, security, reviewer, closer personas |
| `src/coordinare/services/ci_detection.py` | New — detect the repo's CI command from config files |
| `src/coordinare/graph/nodes/monitor_performer.py` | In `_advance_stage`, before transitioning to `monitoring_pr`, run detected CI and route to implementer on failure |
| `agent/performer/src/performer/main.py` | Before committing from any role, run the detected CI command and bail if it fails |
| `tests/unit/services/test_ci_detection.py` | New — test detection against fixture repos |
| `tests/unit/graph/nodes/test_monitor_performer.py` | New — test CI gate transitions |

## Out of Scope

- Replacing the reviewer with a deterministic linter (the AI reviewer
  still has value for semantic issues; the lint run is additive)
- Running CI in a sandbox separate from the performer's workspace
  (use the existing workspace git checkout)
- Fixing up already-broken PRs (this spec prevents the failure mode
  going forward)

## Success Criteria

- [ ] PR #94-style failure (QA-authored test with lint offense) would
      be caught at QA time, not after the tech_writer's docs commits
- [ ] Every persona that commits code explicitly instructs the AI to
      run the project's CI-equivalent checks
- [ ] Coordinare's final `_advance_stage` runs CI as a gate before
      `monitoring_pr`, and routes back on failure
- [ ] 1+ integration test verifying the gate triggers on intentional
      failure
