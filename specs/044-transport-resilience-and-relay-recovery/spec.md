# 044 — Transport Resilience & Relay Recovery

## Summary

Fix the unrecoverable dispatch loop surfaced during live testing: performer stdout contamination from CI check output crashes the transport protocol, session expires, coordinare re-dispatches immediately, and the cycle repeats every 30s burning clones and API quota indefinitely. Five fixes across transport, relay, CI detection, and tech writer.

## Motivation

Live testing on 2026-04-14 with spec 043's CI ownership active. The closer requested changes on PR #94 (card #89). Coordinare relayed to the implementer. The performer's codex backend ran `bundle exec rubocop` (the new CI check), and rubocop's ANSI output leaked to stdout — contaminating the JSON protocol channel. Every `check_status` poll returned garbled bytes, the session expired after ~3 min of transport errors, and coordinare immediately re-dispatched. 6 loops observed before manual shutdown.

## Changes

### 1. Transport: skip non-JSON lines instead of erroring (O-1)

The subprocess transport currently treats any non-JSON byte sequence from the performer's stdout as a `check_status_transport_error`. In practice, performer subprocesses can emit log lines, ANSI escape codes, or diagnostic output from tools (rubocop, bundler, npm) that share the stdout channel.

**Fix:** When parsing a protocol response, if the line doesn't parse as JSON, skip it and try the next line. Only error if no valid JSON line is found in the entire response buffer. Log skipped lines at debug level for diagnosis.

### 2. Relay retry limit with escalation to blocked (O-5, O-2)

When a relay-dispatched performer session expires, coordinare re-dispatches on the next cycle with zero backoff and no retry limit. After N consecutive session expiries on the same card+stage, the card should transition to blocked with a diagnostic message instead of looping forever.

**Fix:** Track `relay_consecutive_failures` in state. Increment on each session expiry during a relay. After 3 consecutive failures, transition to blocked with the transport error details in `open_questions`. Reset the counter when a dispatch succeeds or the card changes.

### 3. CI detection: verify tool is installed before running (O-4)

`ci_detection.detect()` returns lint commands like `bundle exec rubocop` based on file conventions, but the workspace may be a fresh clone without dependencies installed. Running an uninstalled tool produces confusing errors (Bundler errors, "command not found") and — critically — output that contaminates the protocol channel (O-1).

**Fix:** After detecting the lint command, run a quick verification (e.g., `bundle exec rubocop --version` with a 5s timeout). If the tool isn't available, return `lint_command=None` and log a warning. This prevents both false CI failures and stdout contamination.

### 4. Tech writer: batch commits with descriptive messages (O-3)

The tech writer commits each doc file individually with the same generic "docs: update documentation" message. This inflates commit history (14+ commits on PR #94) and makes PRs harder to review.

**Fix:** In the tech writer handler in `performer/main.py`, collect all doc file paths, commit them in a single `git add + commit` with a message like `docs(#{issue_number}): update wiki and card documentation`. Use the existing `commit_file` helper's push mechanism but batch the git add.

### 5. Performer CI check: capture output, don't inherit stdout (O-1 root cause)

The `_run_ci_check` helper in `performer/main.py` uses `run_command()` which properly captures stdout/stderr via `PIPE`. But the codex backend may independently run CI commands via its tool-use capability (responding to the persona directive), and those commands inherit the performer process's stdout — which IS the protocol channel.

**Fix:** Ensure the performer's CI check ONLY runs via `_run_ci_check()` (which captures output), never via the backend's tool-use. Add a note to the persona directive: "Do NOT run CI commands yourself — the coordinare runs them automatically. Focus only on reading and fixing code." This prevents the backend from independently running rubocop and contaminating stdout.

## Files to Change

| File | Change |
|------|--------|
| `src/coordinare/transport/subprocess_transport.py` | Parse response line-by-line, skip non-JSON, log at debug |
| `src/coordinare/graph/nodes/monitor_performer.py` | Track relay_consecutive_failures, escalate to blocked after 3 |
| `src/coordinare/graph/state.py` | Add relay_consecutive_failures field |
| `src/coordinare/graph/nodes/check_board.py` | Reset relay_consecutive_failures on new card |
| `src/coordinare/services/ci_detection.py` | Add tool-availability verification before returning lint command |
| `src/coordinare/services/persona_service.py` | Update persona directives: "do NOT run CI commands yourself" |
| `agent/performer/src/performer/main.py` | Tech writer: batch doc commits into single commit |
| Tests for each change |

## Success Criteria

- [ ] Transport survives non-JSON output mixed into performer stdout without erroring
- [ ] After 3 consecutive session expiries on the same relay, card transitions to blocked (not infinite loop)
- [ ] CI detection skips lint command when tool isn't installed
- [ ] Tech writer produces 1 commit per lifecycle pass (not 14+)
- [ ] Persona directive prevents backend from running CI commands independently

## Out of Scope

- Fully isolating performer stdout from backend tool-use (would require protocol-level changes to the codex/opencode integration)
- Running CI in a sandbox separate from the performer workspace
- Async _advance_stage (follow-up from spec 043 Copilot review)
