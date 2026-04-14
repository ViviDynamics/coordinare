# 043 — Performer CI Ownership — Research

## R-1: Where can CI commands be executed?

**Decision**: Performer-side (primary) + coordinare-side lint gate (fallback).

**Rationale**: The performer's Claude Code backend already runs as a subprocess with full shell access in the cloned workspace. It can execute arbitrary commands via its tool-use capability. No infrastructure changes are needed — just persona instructions telling the AI to run CI before committing. The coordinare-side gate adds defence-in-depth by running lint (fast check only) in `_advance_stage` before transitioning to `monitoring_pr`.

**Alternatives considered**:
- Coordinare-only gate: Rejected — would still allow broken commits, just catch them later. Round-trip penalty for every failure.
- GitHub Actions polling only: Already implemented post-push. But we want to catch failures pre-push, not after.
- New transport-level "run command" API: Over-engineered. The performer already has shell access.

## R-2: How to detect the project's CI command?

**Decision**: Convention-based file inspection — stat the workspace for well-known CI config files and derive the command.

**Rationale**: This is how CI systems themselves work (GitHub Actions looks for `.github/workflows/*.yml`, CircleCI for `.circleci/config.yml`, etc.). No coordinare-side configuration needed. Covers the vast majority of projects.

**Detection table** (priority order — first match wins):

| File(s) present | Lint command | Test command |
|---|---|---|
| `Gemfile` + `.rubocop.yml` | `bundle exec rubocop` | `bundle exec rake test` or `bundle exec rspec` |
| `Gemfile` + `Rakefile` (no rubocop) | — | `bundle exec rake test` |
| `pyproject.toml` + ruff in deps | `ruff check .` | `pytest` |
| `pyproject.toml` + flake8 | `flake8` | `pytest` |
| `package.json` with `"lint"` script | `npm run lint` | `npm test` |
| `package.json` with `"test"` only | — | `npm test` |
| `Makefile` with `lint` target | `make lint` | `make test` |
| `Makefile` with `ci` target | `make ci` | — (ci covers both) |
| `.github/workflows/*.yml` | Parse for lint/test steps | Parse for test steps |

**Fallback**: If no convention matches, return `None`. The gate is skipped, and the persona instruction is the only line of defence. Log a warning so operators know detection didn't fire.

**Alternatives considered**:
- User-configured CI command in `config.yaml`: More flexible but requires setup. Could be added later as an override.
- Parse `.github/workflows/*.yml`: Complex (YAML parsing, matrix expansion, env vars). Deferred — convention detection covers 90%+ of repos.

## R-3: What scope for the coordinare-side gate?

**Decision**: Lint only (fast checks). Full test suite is the performer's responsibility.

**Rationale**: The coordinare gate runs synchronously in `_advance_stage`, blocking lifecycle progression. Running a full test suite (which can take 10+ minutes for Ruby/Rails projects) would stall the lifecycle for every card. Lint commands typically complete in under 30s and catch the exact class of bug that motivated this spec (RuboCop `RSpec/BeEq` on PR #94).

**Alternatives considered**:
- Full test suite in gate: Rejected — too slow. Would need async execution with timeout, adding significant complexity.
- Lint + focused test (just changed files): Interesting but hard to determine safely. Would require parsing `git diff` to find affected test files. Deferred.

## R-4: Where in the performer code to inject CI checks?

**Decision**: In `agent/performer/src/performer/main.py`, before the `commit_file` / push sequence for each role that commits.

**Rationale**: The performer already has a commit path per role (implementer commits code, QA commits tests, security commits security.md, tech_writer commits docs). Injecting a CI-check step before the commit ensures the check runs in the workspace AFTER the AI has made its changes but BEFORE pushing.

Implementation: Add a `_run_ci_check(stand, score)` helper that:
1. Calls `ci_detection.detect(workspace_path)` to get the lint command
2. Runs the lint command via `asyncio.create_subprocess_exec`
3. If it fails, returns the error output so the performer can fix it or bail
4. If no CI detected, skips silently

This hooks into the existing flow without changing the backend interface.

**Alternatives considered**:
- Hook at the transport level (new protocol message): Over-engineered. The performer already has shell access.
- Hook at the coordinare level only: Misses the "fix before push" opportunity — the performer is best positioned to fix lint issues immediately.

## R-5: Persona directive wording

**Decision**: A standard paragraph added to each code-touching persona.

**Wording**:
> Before committing your changes, run the project's CI-equivalent
> commands. For Ruby projects: `bundle exec rubocop` (lint) and
> `bundle exec rspec` (tests). For Python: `ruff check .` and
> `pytest`. For Node: `npm run lint` and `npm test`. If any check
> fails, fix the issue before committing. If you cannot fix it,
> report the failure in your output — do NOT commit code that fails
> lint or tests.

This is descriptive enough that the AI model can apply it regardless of the repo's stack, and specific enough that it knows exactly what commands to try.

**Roles that get this directive**: implementer, qa, security, tech_writer, reviewer (if it auto-fixes), closer (if it auto-fixes).

**Roles excluded**: assessor (no commits), architect (no commits), advocate (no commits).
