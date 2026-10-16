# 223: Fresh sessions for format retries

## Problem and scope

A JSON contract response can arrive with its opening characters missing. Resuming
the damaged backend conversation and quoting the fragment can repeat that failure.
The LiteLLM streaming adapter defect is an infrastructure follow up outside this fix.

The base revision already contains the fresh restart and shape specific prompt
behavior. This change adds explicit structured observability and regression
coverage of every backend that preserves a conversation on relay feedback.

## Required behavior

1. Every format retry stops the backend before starting a fresh backend session
   with the original score, task context and model settings. JSON contract roles
   carry exactly one current `format_repair` entry on `score.relay_feedback`.
   Ordinary relayed feedback remains intact. The performer session ID stays stable.
2. A `head_cut` repair asks for the complete object from its first character and
   required role keys. It includes neither the damaged preview nor its tail.
3. Other shapes retain redacted excerpts. Incomplete endings and invalid syntax
   benefit from showing what needs repair; arrays, wrappers and prose benefit from
   showing what must be reformatted. Empty output is identified as empty. These
   shapes do not show the missing prefix pattern that invites continuation.
4. Preserve `BACKEND_PARSE_RETRIES`, attempt numbering, raw shape logs, redaction,
   capture bounds and exhausted error reporting. Add `fresh_session=true` to the
   existing `backend.invalid_output.retrying` event. Progress also names the fresh
   session. A stop failure remains logged and does not prevent a restart attempt.

## Acceptance and validation

Write tests before changing production code. Cover all JSON contract roles and
aliases against every output shape, repair replacement across attempts, exact
budget exhaustion and capture retention, plus stop failure recovery.

Exercise the actual backend `start` paths with mocked process and network edges:
Claude Code omits the resume session argument, Driver allocates a new session file
without `--resume`, Codex creates a new thread, and OpenCode plus OpenCode Compat
create new HTTP sessions before submitting their repair prompts. Each receives the
original task and the repair and clears the previous terminal status.

Run touched file lint, coordinare mypy, the ruff baseline ratchet, relevant performer
and daemon tests, and the repository node checks. No deployment, commit or browser
test execution is part of this local change.
