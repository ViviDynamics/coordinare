# Return typed work items from the provider instead of GitHub-shaped side tables

Issue #395 (part of epic #393)

## Scope
In:
- `services/work_items.py`: `WorkItem` (id, key, URL, title, body, canonical and
  native status, owners, labels, blockers, linked PR URL) with `Blocker` and the
  GitHub-only `IssueLink`, and `work_items_of(board)`, which reads a provider's
  typed `items` or translates the GitHub Projects listing.
- The GitHub Projects translation covers both blocker syntaxes: the native
  blocked-by relation and the `Depends on #N` text in the body.
- Callers move to the records: `check_board`, `services/dependency.py`,
  `services/todo_waits.py`, `services/orphan_pr.py`, the daemon's board
  metadata mirror. `card_ownership` and `dispatch_performer` already took their
  maps from the caller or from state, which are now filled from the records, as
  is the dashboard's state.
- The dependency check handles a blocker named by item id, with no issue
  number: on the board it waits for Done, off it reads the state the tracker
  reports. Cycles between such items are found by id.
- The in-memory provider lists typed records with labels, owners and blockers.

Out:
- `GitHubService.poll_board` keeps its return shape; the translation lives in
  the provider layer. Changing the service would rewrite every existing fake.
- The card dict still carries `issue_id`, `issue_number` and `issue_url` for
  the code host. Naming an item without an issue number is #397.
- Blocker state for an off-board GitHub issue still comes from
  `check_issue_state`; #396 moves it to the listing.

## Assumptions
- `IssueLink` is the one GitHub-shaped field on the record: the issue behind
  the item, read only by code-host paths and the GitHub dependency syntax.
- A comment about an item with no GitHub issue goes to the item id, and only
  for a provider that lists typed records. A GitHub card with no issue node
  gets no comment, as before.

## Tasks
- [x] 1. Typed records and the GitHub listing translation.
- [x] 2. Dependency check on the records, with item-id blockers.
- [x] 3. Callers on the records; an AST test keeps them off the side tables.
- [x] 4. In-memory provider lists typed records.
