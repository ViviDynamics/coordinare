# Let the architect persona run on Progettare, opt-in

Issue #403

## Scope
In:
- Generalise the #402 stage contract for a stage whose input is a work item, not a
  pull request: a `WorkItemRequest` (repository, issue number and node, and a pin over
  what the planner reads) and a `WorkItemStageService` that reuses the adapter's attempt
  walk, retry and block rules. The reviewer path and its tests are unchanged.
- A Progettare provider (`services/progettare.py`) for the architecting stage. It runs
  the released CLI at a pinned version, `progettare blueprint`, in a detached runner
  (`services/progettare_runner.py`) that clones the repository and holds a lock for the
  life of the run. The request id names the run's directory, so a restart or a
  re-dispatch finds the same run.
- Map the run onto the contract: `complete` passes the stage as `plan_committed` with a
  spec-165 blueprint built from Progettare's briefs (implementer milestones, QA
  criteria, documenter topics, size); `blocked` blocks the card with Progettare's
  questions; `failed`, an interrupted run, or a run that never started is a retryable
  system error. A record whose contract version is not 1 is refused.
- Config: a `progettare` kind in `stage_providers` (`command`, `version`, `runs_dir`,
  `config_path`, `gh_token_env`, `pass_env`, `timeout_seconds`), validated at load, and
  `performers.architect.provider`.
- `doctor` and the startup preflight: the CLI is installed at the pinned version, the
  GitHub token variable is set, the runs directory is usable, the config file exists.
- A card whose work item is not a GitHub issue blocks at dispatch with a reason that
  names ViviDynamics/progettare#68.
- Docs and `config.example.yaml`.

Out:
- Planning work items that are not GitHub issues. Needs ViviDynamics/progettare#68.
- The MCP server and the GitHub App as front doors. The App is triggered by a label
  coordinare must never apply; the MCP tool cannot set the runs directory or config.
- Committing a rendering of the blueprint. The spec-165 workflow architect commits
  nothing either; the blueprint is held on the session and briefs are cut from it.
- Changes in the Progettare repository. Its API gaps are filed as
  ViviDynamics/progettare#69.

## Assumptions
- The CLI posts nothing to GitHub and commits nothing, so no failed attempt can have
  delivered anything: every failure is safe to retry under the next attempt.
- The pin is the issue's title and body plus the clarifications coordinare recorded.
  Feedback routed back to the architect is not pinned: Progettare cannot read it, so, as
  for Scrutare, it is logged as not forwarded and the plan of the unchanged issue stands.
  Blocking on it, or pinning it, were both tried and left state a person could not clear.
  An edited body or a new answer is a new request, so it starts a new run; a restart
  with neither finds the same run. Progettare reads acceptance criteria from the body,
  so an intake question is answered by editing the body.
- The plan belongs to the issue text it was made from: when a run finishes, the title
  and body it recorded in `intake.json` must match the issue's current ones, or the
  result is refused and the current text is planned.
- The runner is detached from the daemon, so a daemon restart leaves it running and
  the next poll finds it by its lock. If the host or container restarts, the run dies,
  its lock is free and it has no outcome: that attempt is retried, and still only one
  plan is produced.

## Tasks
- [x] 1. Config: the progettare kind, validation, role fit, form catalog
  (tests/unit/test_403_progettare_config.py).
- [x] 2. Work-item contract: request id, pin, session, finish check, blocked questions,
  non-GitHub refusal (tests/unit/test_403_work_item_stage.py).
- [x] 3. Progettare provider and runner against a fake CLI: start, find, lock, outcomes,
  contract refusal, blueprint mapping, health (tests/unit/test_403_progettare_provider.py).
- [x] 4. Wiring: service build, routing, doctor (tests/unit/test_403_progettare_wiring.py).
- [x] 5. Docs and config example.
