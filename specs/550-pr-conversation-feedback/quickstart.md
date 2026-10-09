# Interaction checks

Use the isolated sample project after all six follow-ups ship. Record the deployed version and current PR head before testing.

1. Post an ordinary PR comment from a configured human requesting one narrow regression. Verify one classification with source URL, one performer payload and the resulting head/test change.
2. Poll unchanged comment repeatedly; restart after dispatch. Verify replacement retains its instructions and no duplicate completed turn.
3. Post acknowledgement and approval-like conversation text; verify no dispatch or merge authorization.
4. Edit the request to add a second narrow regression; verify one additional turn. Edit without changing the body; verify no new turn.
5. Post two requests from the same human and a submitted inline review; verify all independent requests remain represented.
6. Exercise an untrusted author/bot, fetch failure and bounded catch-up in automated tests.

Automated coverage: `PYTHONPATH=src:packages/service_inference/src .venv/bin/pytest tests/unit/test_550_pr_conversation_feedback.py`.
This runs the real monitor, classifier, and performer-dispatch path against
controlled services, plus complete pagination, edit/revert, mixed review/comment
feedback, inference/fetch cancellation, classification limits, and approval
deferral while conversation updates remain unread. Snapshot schema and actual
restart tests are integrated separately after the other lifecycle follow-ups.
