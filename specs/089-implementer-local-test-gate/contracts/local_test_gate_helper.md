# Contract: `_run_test_check` performer helper

**Module**: `agent/performer/src/performer/main.py`
**Mirrors**: `_run_ci_check` (lines 100–135)

## Signature

```python
async def _run_test_check(
    stand_path: Path,
    *,
    timeout_seconds: int = 600,
    label: str = "performer",
) -> LocalTestResult:
    ...
```

## Behaviour contract

| Precondition | Action | Result |
|--------------|--------|--------|
| `coordinare` package not importable (standalone) | catch `ImportError` | `LocalTestResult(passed=True, command=None, ...)` — pass-through (FR-012) |
| `detect(stand_path).test_command is None` | log `test_check.no_test_detected` | `LocalTestResult(passed=True, command=None, ...)` — skip (FR-001) |
| test command runs, exit 0 | log `test_check.passed` | `LocalTestResult(passed=True, command=<cmd>, duration=…)` |
| test command runs, non-zero exit, **no** env signal | log `test_check.failed` | `LocalTestResult(passed=False, env_blocked=False, output=<tail>, command=<cmd>)` |
| test command non-zero/timeout, **and** `consume_services_start_failure()` or `consume_env_cache_health_failure()` fired | log `test_check.env_blocked` | `LocalTestResult(passed=False, env_blocked=True, env_reason=<reason>, output=<tail>)` |
| test command times out, no env signal | log `test_check.timeout` | `LocalTestResult(passed=False, env_blocked=False, output=<timeout note + tail>)` (genuine failure, FR-010) |

## Invariants

- Env-signal consumers are called **after** the test run completes (so a services-start/health failure that occurred during the run is observed) and **only on the failure branch** (a passing run pushes regardless — spec edge case "env failure but tests pass").
- The helper never raises for a test failure; failures are returned as data.
- `output` is the combined stderr+stdout tail; callers truncate to ≤500 chars for PR/feedback bodies.
- Observability event per outcome (FR-013) includes `command`, `duration`, `label`, and the classification.

## Test obligations

- pass-through when coordinare unavailable (monkeypatch import to raise).
- skip when `test_command is None`.
- green run → `passed=True`.
- red run, no signal → `passed=False, env_blocked=False`.
- red run + `consume_services_start_failure()` returns text → `env_blocked=True`, `env_reason` set.
- red run + `consume_env_cache_health_failure()` True → `env_blocked=True`.
- timeout, no signal → `passed=False, env_blocked=False`.
- green run + env signal present → `passed=True` (signal not consulted / push proceeds).
