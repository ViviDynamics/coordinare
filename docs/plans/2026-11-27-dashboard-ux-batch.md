# Dashboard UX batch: nav labels, Done cards, stage dots, the approver setting, and the logins field

Issue #365

Shipped in two pull requests. The first carries (a), (c), (d) and (e). The second carries
(b), archiving Done work items on the GitHub project, and leaves nothing of #365 open.

## Scope

In (first pull request):
- (a) One **Activity** nav entry replaces History and Logs. `/activity/history` and
  `/activity/logs` are its two views, with a switch between them and one line on each
  saying what it holds. `/activity` opens History. `/history` and `/logs` redirect, with
  their query string, on the server and in the in-app router.
- (c) The stage dots on a board card get a visible caption: the current stage's name and
  its place in the pipeline ("Stage 5 of 8: Review"). Each dot keeps its tooltip, and the
  strip's accessible name lists every stage with its status.
- (d) `human_reviewers` gets the label "Who can approve merges (human_reviewers)" and help
  text that states the rule: only these logins can approve a merge, for every symphony; an
  approval from any other login shows on the work item and does not merge. The symphony
  settings form gets a line that says the same and links to the field; a symphony that
  sets its own list says so.
- (e) The logins fields (`human_reviewers`, `trusted_bot_reviewers`) show the parsed list
  under the input as it is typed. Commas, spaces and new lines all separate logins. An
  entry that cannot be a GitHub login is flagged, and Save refuses it with a field error.
  The stored config stays a YAML list of strings.
- The Settings page's one-second reload after a save kept the "Saved" message only by
  luck. The reload now hands the message to the redrawn section, as #411 did for the
  symphony form.

In (second pull request):
- (b) Work items in Done for at least `archive_done_after_days` (default 14, `0` turns it
  off) are archived on the GitHub project, inside the GitHub Projects provider. The board
  says so under the Done lane.

Out:
- Checking that a login exists on GitHub. Jason's decision for (e) asks for an
  unambiguous format and the parsed list. An existence check needs a GitHub call from the
  Settings page for every edit; a typo of a valid login is still caught on the work item,
  where #407 shows `approval_from_unlisted_reviewer`.
- A per-symphony approver field in the symphony form. The rule is global by decision;
  a symphony override in YAML still works and is shown.

## Assumptions

- The Activity views keep the existing page elements (`#history-page`, `#logs-page`), so
  their scripts and tests keep their hooks. Only the path checks move.
- "Stage N of M" counts the strip's own stages, which are the symphony's lifecycle.

## Tasks

- [x] 1. Activity routes: server redirects for `/history`, `/logs`, `/activity`
      (unit test on the routes), client router and path checks (node checks), nav and
      switch markup (E2E).
- [x] 2. Stage strip caption (timeline node checks, E2E on the board card).
- [x] 3. `human_reviewers` label and help (descriptor unit test), symphony form line
      (node check, E2E).
- [x] 4. Logins input: parse, flag, preview, refuse on save (node checks, E2E).
- [x] 5. Settings "Saved" survives the reload (node check, E2E).
- [ ] 6. (b) archive policy, config, provider sweep, board text (second pull request).
