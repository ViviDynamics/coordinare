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

## Second slice: the remaining writes and the blocker read

The owner approved changing the tests that pin these sites, on one condition:
each changed test keeps checking the same outcome (the same comment text, the same
close, the same blocker result) and expects it through the work item provider,
where the GitHub Projects provider makes the same GitHub call. No other existing
test changes.

### Scope
In:
- A "not work" move target on the seam (`NOT_WORK` in `board_provider.py`), a
  state and not an operation, so the protocol keeps its five operations. GitHub
  Projects closes the issue and moves the item to Done. The in-memory provider
  moves the item to its Done state.
- The assessor's decline comment goes through `add_card_comment`, the `not_work`
  close through `move_card(card, NOT_WORK)`. `test_410_decline_outcomes` wraps
  its fake GitHub service in the GitHub Projects adapter and checks the same
  comment, close and Done move on that service.
- The QA evidence comment. Coordinare tells the performer it posts work item
  comments (`work_item_comments_by_coordinare` on the score). The performer
  then returns the comment it built (`work_item_comment` on the response) and
  does not post it; `monitor_performer` posts it with `add_card_comment` the
  first time it reads the terminal QA status. A performer dispatched by an
  older coordinare, which does not set the flag, posts it itself as before, so
  a version skew between daemon and performer image never posts it twice or
  drops it.
- Blocker state from the listing. The GitHub Projects adapter reads the state of
  each off-board issue that a To Do card's body names with the text syntax
  (`check_issue_state`, the same call) while it lists the board, and the
  listing carries it on each blocker. To Do only, because that is where the
  dependency filter acts; reading every card's blockers would spend a REST call
  per poll on done cards. `resolve_off_board_dependencies` uses the
  listed state and asks GitHub only for blockers the listing does not carry
  (those declared in issue comments).

Out:
- Comment-declared blockers (091) still read issue comments and, for an
  off-board blocker, its state from GitHub. They are the GitHub dependency
  syntax in GitHub issue comments; nothing in the listing names them.

### Tasks
- [x] 3. `NOT_WORK` on the seam: adapter closes and moves to Done; in-memory moves to Done; conformance case.
- [x] 4. Decline path through the provider; `test_410_decline_outcomes` expects it through the adapter.
- [x] 5. QA evidence comment: score flag, response field, daemon posts once through the provider.
- [x] 6. Blocker state on the listing; dependency service reads it first.
- [x] 7. Guard: drop the decline allowances, narrow the QA allowance to the older-coordinare path.
