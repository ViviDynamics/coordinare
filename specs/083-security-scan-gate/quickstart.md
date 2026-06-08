# Quickstart: Security Scan Gate

How to validate spec 083 end-to-end and per-component. TDD RED-first throughout.

## Prerequisites

```bash
set -a && source .env && set +a          # config ${VAR} placeholders expand at load time
.venv/bin/pytest --version               # use .venv/bin/pytest, NOT python -m pytest
```

## Run the unit suites (per contract)

```bash
# P1 — scanner
.venv/bin/pytest tests/unit/services/test_security_scanner.py -v

# P1 — floor enforcement (carries the 082 regression)
.venv/bin/pytest tests/unit/graph/nodes/test_monitor_performer.py -v -k security

# P1 — dispatch integration
.venv/bin/pytest tests/unit/graph/nodes/test_dispatch_performer.py -v -k security

# P2 — persona
.venv/bin/pytest tests/unit/test_persona_service.py -v -k security

# P3 — config denylist
.venv/bin/pytest tests/unit/ -v -k "config and security"
```

## Lint

```bash
.venv/bin/ruff check src/coordinare/services/security_scanner.py \
  src/coordinare/graph/nodes/dispatch_performer.py \
  src/coordinare/graph/nodes/monitor_performer.py \
  src/coordinare/services/persona_service.py \
  src/coordinare/config.py
```

## Acceptance bar (SC-001) — 082 vulnerable bench PR

The whole feature passes only when the 082 vulnerable bench PR, re-run through the gated
security role with a model that returns `passed: true`, yields **`security_failed`**.

1. Build the performer `Dockerfile.full` so semgrep + bandit are present (advisory tool):
   ```bash
   # base FIRST (source-COPY invariant), then full
   docker build -f agent/performer/Dockerfile.base -t performer:base .
   docker build -f agent/performer/Dockerfile.full -t performer:full .
   ```
2. Dispatch the security role against the 082 bench PR. Expected:
   - coordinare scans the diff once at dispatch → `state["scanner_findings"]` has a critical/high
     finding;
   - even if the model returns `passed: true`, the monitor floor forces `security_failed`;
   - scanner findings appear in `relay_feedback`.

## Fail-closed smoke check (SC-003)

Simulate scanner-unavailable (e.g. tool missing / forced error in a test double):
- expect `security_failed` with a synthetic `critical` `scanner_unavailable` finding,
  `routing: halt`, and a loud observability marker — never a silently-absent floor.

## Secrets discipline check (SC-005)

Grep INFO-level log output produced during a scan run — assert NO raw diff content and NO
`auth_env`-resolved values appear; only scan summary (counts/severities).

## Success criteria mapping

| SC | Validated by |
|----|--------------|
| SC-001 | 082 bench PR re-run → `security_failed` (acceptance bar above) |
| SC-002 | `test_monitor_performer.py` override test (critical/high + model pass → fail) |
| SC-003 | fail-closed smoke check + unit test |
| SC-004 | `test_config*.py` denylist load test |
| SC-005 | secrets-discipline log grep + INFO-summary-only assertion |
