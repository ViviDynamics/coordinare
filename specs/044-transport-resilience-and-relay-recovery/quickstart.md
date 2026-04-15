# 044 — Transport Resilience & Relay Recovery — Quickstart

## What changes for operators

**Nothing to configure.** All fixes are internal:

- Transport is now resilient to non-JSON stdout noise from performer subprocesses
- Relay failures auto-escalate to blocked after 3 consecutive session expiries (with diagnostic in open_questions)
- CI detection verifies tools are installed before trying to run them
- Tech writer produces 1 commit per pass instead of 14+

## What changed from the dispatch loop incident

The incident: closer requested changes → implementer relay → codex backend ran `bundle exec rubocop` via tool-use → rubocop output leaked to stdout → transport failed to parse → session expired → coordinare re-dispatched immediately → infinite loop.

Now: transport skips non-JSON lines → even if rubocop leaks, the next valid JSON response is accepted. And if sessions still expire, the retry budget stops the loop after 3 attempts.

## Testing

```bash
# Transport resilience
.venv/bin/pytest tests/unit/transport/ -q

# Relay retry budget
.venv/bin/pytest tests/unit/graph/nodes/test_monitor_performer.py -q

# CI tool verification
.venv/bin/pytest tests/unit/services/test_ci_detection.py -q

# Tech writer batch commits
.venv/bin/pytest agent/performer/tests/unit/test_workspace.py -q

# Full suite
.venv/bin/pytest tests/ -q && .venv/bin/pytest agent/performer/tests/ -q
```
