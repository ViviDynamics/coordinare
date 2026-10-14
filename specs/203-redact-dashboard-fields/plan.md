# Implementation Plan: Redact performer text in every dashboard field

**Spec**: `specs/203-redact-dashboard-fields/spec.md`

## Approach

Redact at the writer, not in the serialiser. The snapshot builder reads state, so once state holds the redacted form, every viewer (SSE, `/api/state`, MCP, the board comment, notifications) receives it without a second pass.

1. **One helper.** `redact_state_value(value)` in `lib/redaction.py` applies `redact_command_text` to a string and walks lists and dicts. It is linear because `redact_command_text` is linear.
2. **Entry points that are functions.** The observer's `parse_verdict` redacts the judge's reason before it is capped, and `_summarise_evidence` redacts each string before its cap. The subprocess transport's `_drain_stderr` redacts each line as it is buffered, and `_fetch_session_stats` redacts the title.
3. **Writers that assign fields.** About 100 assignments, dict entries and keyword arguments set the text fields across the graph nodes, the services and the daemon. Each value is wrapped in `redact_state_value(...)`. The wrap was applied by a one-off AST rewrite that splices the wrap at each node's exact byte span, so the edit is mechanical and nothing else on those lines moves. A `None` or an empty container is left alone.
4. **Raw copies kept where functional.** The local test gate's raw failing output still goes into `relay_feedback`, which is the implementer's self-fix prompt. Only the escalated `open_questions` and `system_error_reason` copies are wrapped.
5. **Guards.** Two checks.
   - A static guard walks the writer modules' AST and fails on any unwrapped assignment, dict entry or keyword to a text field.
   - A dynamic guard drives the real entry points with secrets in each shape, builds the snapshot, walks the whole JSON, and runs the MCP state tool over the same snapshot.

## Files

- `src/coordinare/lib/redaction.py`: `redact_state_value`.
- `src/coordinare/services/observer.py`: judge reason and evidence.
- `src/coordinare/transport/subprocess_transport.py`: stderr buffer and session title.
- `src/coordinare/graph/nodes/**`, `src/coordinare/services/**`, `src/coordinare/daemon.py`, `src/coordinare/cancel.py`: wrapped writers (AST rewrite).
- `tests/unit/test_203_dashboard_redaction.py`: the helper, the entry points, the dynamic snapshot and MCP walk, and the static writer guard.

## Constraints kept

- No function-level imports, no `# type: ignore`, no `# noqa`, no skips.
- No new regexes. The transcript redactor and the client-form scan are unchanged.
- The dashboard serialisers (`dashboard/store.py`) are unchanged.
- No schema change. Snapshots written before this change still load. A restored raw value passes through the daemon's restore writers, which are wrapped, so it is redacted on the next write.

## Known limits

- A secret that is only in a job's payload, and never appears in a pattern, flag or assignment form in the output, is not caught. The daemon does not hold those exact values at the writer.
- Human text (issue titles, human clarification answers) is out of scope. See the spec.
