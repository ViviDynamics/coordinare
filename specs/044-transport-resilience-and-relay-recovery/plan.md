# Implementation Plan: 044 — Transport Resilience & Relay Recovery

**Branch**: `044-transport-resilience-and-relay-recovery` | **Date**: 2026-04-14 | **Spec**: `specs/044-transport-resilience-and-relay-recovery/spec.md`

## Summary

Fix the unrecoverable dispatch loop: transport crashes on non-JSON stdout, relay re-dispatches infinitely, CI detection runs uninstalled tools. Five targeted fixes across transport parsing, relay retry budgets, CI tool verification, persona directives, and tech writer commit batching.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: structlog, pydantic, asyncio (all existing)
**Storage**: N/A — state fields only (in-memory)
**Testing**: pytest (existing)
**Target Platform**: Linux / macOS
**Performance Goals**: Transport parsing adds < 1ms overhead per line; CI tool verification < 5s
**Constraints**: Must not break the existing protocol contract — valid JSON responses must parse identically to before

## Constitution Check

| Principle | Status | Notes |
|---|---|---|
| I. Code Quality First | ✅ | Fixes a code quality regression (stdout contamination from 043) |
| II. Testing Discipline | ✅ | Each fix has targeted tests |
| III. User Experience | ✅ | Eliminates the infinite-loop compute burn visible in Slack notifications |
| IV. Performance by Design | ✅ | Parsing overhead bounded; CI verification has 5s timeout |
| V. Clarity Before Action | ✅ | All 5 oddities documented with reproduction steps from live testing |

No violations.

## Architecture Decisions

### AD-1: Line-skipping over JSON extraction for transport resilience

Two approaches to handle non-JSON stdout:
1. **JSON extraction** (search for `{...}` substrings in each line) — fragile, could match JSON-like log output
2. **Line-skipping** (skip non-JSON lines, accept first valid JSON) — simpler, matches the protocol's line-delimited design

Chosen: **Line-skipping**. The performer protocol is line-delimited JSON (one JSON object per line, terminated by `\n`). Non-JSON lines are diagnostic noise. Skip them, log at debug, accept first valid JSON. If no valid JSON found after all lines consumed, raise TransportError as before.

### AD-2: Reuse system_error_count for relay retry budget

Rather than adding a new `relay_consecutive_failures` field, reuse the existing `system_error_count` which already increments on transport errors. Add a threshold check: when `system_error_count >= 3` during a relay (detected by `relay_feedback` being non-empty), transition to blocked instead of re-dispatching.

This avoids new state fields and leverages existing infrastructure. The count resets on successful dispatch (already implemented).

### AD-3: CI tool verification via --version check

Before returning a lint command, run `{tool} --version` with a 5s timeout. If it exits non-zero or times out, set `lint_command=None`. This catches: tool not installed, bundler not configured, wrong Ruby version, etc.

Verification runs synchronously in `detect()` (blocking but bounded to 5s). Acceptable because detection runs once per lifecycle stage, not per poll cycle.

### AD-4: Batch commit via workspace helper

Add a `commit_files(stand, files, message)` helper that does `git add` for all files, then single `git commit`, then single `git push`. The tech writer handler calls this instead of N individual `commit_file` calls.

## Project Structure

```text
src/
├── coordinare/
│   ├── transport/
│   │   └── subprocess_transport.py  # Updated: resilient line-by-line parsing
│   ├── graph/nodes/
│   │   └── monitor_performer.py     # Updated: relay retry budget
│   └── services/
│       ├── ci_detection.py          # Updated: tool verification
│       └── persona_service.py       # Updated: "don't run CI yourself"

agent/performer/
└── src/performer/
    ├── main.py                      # Updated: tech writer batch commits
    └── workspace.py                 # Updated: commit_files batch helper
```

## Files Changed

| File | Changes | Effort |
|------|---------|--------|
| `src/coordinare/transport/subprocess_transport.py` | `_parse_response`: line-by-line skip non-JSON, accept first valid | M |
| `src/coordinare/graph/nodes/monitor_performer.py` | Relay retry budget: blocked after 3 session expiries | S |
| `src/coordinare/services/ci_detection.py` | `_verify_tool(cmd, cwd)` before returning lint_command | M |
| `src/coordinare/services/persona_service.py` | "Do NOT run CI commands yourself" in committer personas | S |
| `agent/performer/src/performer/main.py` | Tech writer: use `commit_files` batch helper | S |
| `agent/performer/src/performer/workspace.py` | New `commit_files(stand, files, message)` helper | S |
| Tests for each change | | M |

~250 lines across 7 files. 5 commits.
