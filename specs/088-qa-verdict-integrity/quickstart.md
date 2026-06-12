# Quickstart: validating 088 (QA verdict integrity & env reliability)

## Unit-level (fast, no containers)

```bash
# Cluster A — verdict integrity (performer package)
.venv/bin/pytest agent/performer/tests/ -q -k "qa_verdict or unsubstantiated or env_blocked or app_boot or visual"
# The PR #159 replay fixture is the canonical SC-001 check:
.venv/bin/pytest agent/performer/tests/ -q -k "pr159_replay"

# Cluster B — env policy conformance (all backends)
.venv/bin/pytest agent/performer/tests/unit/backends/test_env_policy.py agent/performer/tests/unit/backends/ -q -k "env_policy or subprocess_env or cache_path"

# Coordinare side — verdict gate, circuit breaker, restart-resume, secret refresh
.venv/bin/pytest tests/unit/graph/nodes/test_monitor_performer*.py tests/unit/test_060_env_cache.py tests/unit/services/test_http_performer_service.py -q

# Full gates (mirror CI)
.venv/bin/ruff check src tests agent
.venv/bin/pytest --cov=coordinare --cov-report=term-missing --cov-fail-under=90 -q
.venv/bin/pytest agent/performer/tests/ -q --ignore=agent/performer/tests/integration
```

## Deployment order (compatibility)

1. Merge + restart **coordinare** first (handles `qa_env_blocked`, circuit breaker, restart-resume).
2. Rebuild performer images (`docker build -t coordinare-performer:base -f agent/performer/Dockerfile.base . && docker build -t coordinare-performer:full -f agent/performer/Dockerfile.full agent/performer/`) — ephemeral performers pick up the new image on next spawn; no coordinare restart needed.

## Live validation scenarios

1. **SC-001 false-pass replay**: dispatch a qa card into a container with a deliberately broken toolchain (e.g. temporarily rename the cache's `.rbenv`); expect a `qa_env_blocked` hold with the blocker named in the PR comment — never "PASSED".
2. **SC-002**: `grep -rn 'PATH' agent/performer/src/performer/backends/*.py` shows PATH composition only in `_env_policy.py`.
3. **SC-003 circuit breaker**: point a symphony at spec files demanding an impossible runtime (e.g. ruby 9.9.9); expect exactly 3 bootstrap dispatches with growing gaps, one `bootstrap_exhausted` notification, then silence; consumer holds name the exhaustion.
4. **SC-004 restart-resume**: after a successful bootstrap, restart coordinare (`set -a && source .env && set +a && PYTHONUNBUFFERED=1 nohup uv run python -m coordinare --config "$PWD/config.yaml" >> /tmp/coordinare.log 2>&1 &`); expect `env_cache.clean_verify_result passed=True` and consumer dispatch within ~2 min, with **no** `env_cache.bootstrap_dispatched` line.
5. **SC-005 observability**: kill the token-mint path mid-session / make services-start.sh exit 1; expect `http_performer.secret_refresh_degraded` / `env_cache.services_start_failed` (error level) with actionable fields.
6. **SC-006 no-regression**: run an ordinary card end-to-end on a healthy cache; verdicts and PR comments unchanged from today.
