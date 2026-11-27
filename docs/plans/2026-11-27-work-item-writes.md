# Route every work item write through the provider

Issue #396 (part of epic #393). A slice: the issue stays open.

## Scope
In:
- The output token limit notice (`graph/nodes/monitor/body.py`) is posted with
  `board_provider.add_card_comment`, so it reaches the configured tracker.
- A guard test: any issue write (`add_comment`, `close_issue`, `add_labels`,
  `ensure_labels_exist`, `post_issue_comment`) from a graph node, or from
  performer role code outside the advocate and curator, fails unless its site is
  listed with a reason. Pull request comments are listed as code-host writes.
- The GitHub Projects adapter keeps the id it was given when a stand-in for the
  service answers the id lookup with something that is not an id.

Out, and why:
- The assessor's decline comment and the `not_work` close in
  `graph/nodes/monitor/ui.py`. `tests/unit/test_410_decline_outcomes.py` calls
  the decline path with a separate board and GitHub service and asserts that the
  comment and the close land on the GitHub service. Routing them through the
  provider fails that test, and the epic forbids editing an existing test.
- The QA evidence comment the performer posts on the issue.
  `agent/performer/tests/unit/test_main.py` asserts the performer calls
  `post_issue_comment` itself, once.
- A "not work" state on the seam. Its only caller would be the decline path
  above.
- Blocker state from the listing. The GitHub off-board check runs from
  `check_board` with `check_issue_state`, and existing tests assert that call.

## Tasks
- [x] 1. Token limit notice through the provider.
- [x] 2. Guard over graph nodes and performer roles, with the remaining sites listed.
