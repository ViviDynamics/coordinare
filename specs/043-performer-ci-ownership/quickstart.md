# 043 — Performer CI Ownership — Quickstart

## What changes for operators

**Nothing.** CI detection is convention-based and automatic. If your repo has a `Gemfile` + `.rubocop.yml`, the performer will run `bundle exec rubocop` before committing. If it has a `pyproject.toml` with ruff, it'll run `ruff check .`. No configuration needed.

## What changes for AI performers

Every code-touching performer persona now includes an instruction to run CI locally before committing:

- **Implementer**: runs lint + tests after implementing
- **QA**: runs lint + tests after adding test files
- **Security**: runs lint after adding patches
- **Tech writer**: runs lint after editing docs
- **Reviewer / Closer**: runs lint if they auto-fix anything

If lint or tests fail, the performer fixes the issue or reports it as blocked — never commits a broken tree.

## What changes in the coordinare lifecycle

A new CI lint gate runs in `_advance_stage` before the final transition to `monitoring_pr`. If the lint command fails:
1. The card routes back to the implementer with the lint failure in `relay_feedback`
2. The implementer fixes the issue
3. The lifecycle re-runs through the remaining stages
4. On the next pass, the gate succeeds and the card moves to `monitoring_pr`

This is defence-in-depth — the performer should have caught the issue already. The gate catches the case where the persona instruction was ignored.

## Supported stacks

| Stack | Detection | Lint | Tests |
|---|---|---|---|
| Ruby | `Gemfile` + `.rubocop.yml` | `bundle exec rubocop` | `bundle exec rspec` |
| Python | `pyproject.toml` + ruff | `ruff check .` | `pytest` |
| Node | `package.json` + `lint` script | `npm run lint` | `npm test` |
| Make | `Makefile` + `lint`/`ci` target | `make lint` | `make test` |
| Unknown | No match | Skipped (warning logged) | Skipped |

## Testing locally

```bash
# Run coordinare tests to verify CI detection
.venv/bin/pytest tests/unit/services/test_ci_detection.py -v

# Run the full suite (includes gate transition tests)
.venv/bin/pytest tests/ -q
```
