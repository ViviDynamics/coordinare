# Implementation plan — PR conversation feedback

Issue #550, branch 550-pr-conversation-feedback.

## Technical context and constitution check
Python 3.12+, existing asyncio/httpx/GitHub REST service and graph feedback classifier. No dependency additions. Tests precede product code. Existing reviewer authorization remains authoritative. Existing ship-issue standing rules govern review, required checks and squash merge; no new approval policy. Scope and purpose explicitly authorized by user.

## Design
Add a dedicated PR-conversation polling helper, invoked by monitor_pr before selecting merge or feedback. Fetch comments through the PR number with edit metadata. Keep submitted reviews separate: same-author supersession applies only to review objects. Classify authorized conversation requests with a bounded inference call and deterministic fallback; acknowledgements/approval/noise create no work. Requests become explicit feedback items carrying source/comment ID/URL/author/version, and ride existing durable relay/in-flight feedback.

Persist per-card PR comment tracking (schema30, after #549 schema27, #548 schema28 and #552 schema29). A content digest skips unchanged bodies; a changed body gets an identity combining PR ID, comment ID, updated_at and body digest (including A→B→A changes). Require complete API pagination, then sort by updated_at before advancing the update watermark through a fully handled chronological prefix, with an overlap for GitHub's second-resolution timestamps. Pending requests must be committed to durable relay state before tracking acknowledges them. Failure or budget expiry leaves unread entries eligible. A changed PR identity starts an independent stream.

## Files and tests
- services/github.py: dedicated comment API or backward-compatible shared fetch with updated_at/html_url; paginated errors remain fail-safe.
- services/pr_conversation_feedback.py: authorization, bounded polling/classification, provenance, durable acceptance.
- graph/nodes/monitor_pr.py: integrate conversation requests after review supersession and before merge selection; preserve closed-PR guard from #551.
- graph/state.py, session.py, state_store.py, daemon.py: tracking persistence, defaults, hydration and retirement.
- tests/unit/test_550_pr_conversation_feedback.py: request/noise/approval/untrusted, edit/revert/unchanged, same-author multiple requests, submitted-review coexistence, restart/failure/budget, API payload and performer delivery.
- specs/003-state-persistence/contracts/workflow-snapshot.schema.json and version assertions: schema30.
- operator docs: channel behavior, edit handling, no comment-based merge authorization.

## Performance and recovery
Use a 20-second wall-clock budget and five classifications per tick, consistent with issue comment routing. Test budget handling with controlled clocks. Fetch updates with GitHub since semantics and overlap, preserving unread chronological suffix. Failed or incomplete pagination preserves the watermark and versions entirely; catch-up retries the same overlapped fetch on subsequent cycles without dispatching a partial page. Do not persist secrets, endpoints, runner payloads, or raw API credentials.

## Dependencies
Deliver #547–#552 in one batch PR on this branch. The shared snapshot, routing, lifecycle and pause paths must pass preflight and CI as the exact deployed combination. The isolated integration audit exercised 978 focused and adjacent tests, including confirmed/uncertain pause capacity and closed-PR resume guards. Each issue retains its review history, budgets and shipping result. Individual PRs #553–#555 are superseded after the batch merges.
