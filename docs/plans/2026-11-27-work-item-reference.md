# Name a work item in branches, pull requests and plan paths without a GitHub issue number

Issue #397 (part of epic #393)

## Scope
In:
- The provider supplies a reference for each item on the typed record:
  `reference`, a token safe in a branch name and a path, and `pr_line`, the line
  a pull request body carries. GitHub Projects: the issue number and
  `Closes #N`. The in-memory provider: the item key and a link to the item.
- A card for an item with no GitHub issue carries `work_item_ref` and
  `work_item_pr_line`. A GitHub card carries neither, so its dispatch payload is
  unchanged.
- The performer's score takes both. The pull request body carries the line in
  place of `Closes #N`, and plan and report files go under
  `docs/cards/{ref}-{slug}`. With an issue number nothing changes.
- The branch is already `coordinare/{card id}/{slug}`, so it carries the key.
- Pull request recovery (`monitor_pr`, when state lost the link) finds an
  item's open pull request by branch prefix when the card has a reference, and
  by closing references otherwise, as before.

Out:
- The protocol's JSON schema: the checked-in contract file already differs
  from the generated one on main. The dispatch contract table documents the two
  fields.
- Other `find_pr_for_issue` sites (assessment's open PR check, blocked recovery,
  dispatch's divergence checks) keep closing references.

## Tasks
- [x] 1. Reference on the record, the card and the score.
- [x] 2. Pull request body and docs path from the reference.
- [x] 3. Recovery by branch prefix for an item with no issue.
