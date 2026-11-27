# Stage providers

A stage provider is an outside service that fills one lifecycle stage in place of a
performer. Coordinare hands it a pull request and a head, and takes back a verdict. The
first provider is [Scrutare](https://github.com/ViviDynamics/scrutare) for the reviewing
stage (issue #402). [Progettare](https://github.com/ViviDynamics/progettare) fills the
architecting stage (#403), where the input is a work item instead of a pull request. QAre
(QA, #404) is meant to use the same contract.

Nothing changes until a role names a provider. A config without `stage_providers`, or
whose roles name none, runs every stage on performers exactly as before.

## The stage contract

Every provider fills the same contract (`src/coordinare/services/stage_contract.py`):

| | |
| --- | --- |
| Input | the repository, the pull request, and the head SHA to judge |
| Output | a verdict (`pass`, `changes_requested` or `needs_human`), the findings, the head that was actually judged, and a reference to the record |

A stage that plans (architecting) takes a work item instead: the issue, pinned to the text
the planner reads (its title and body, and the clarifications coordinare recorded). Its
output is a blueprint, and the verdict belongs to the issue text it was planned from. See
[Progettare for the architecting stage](#progettare-for-the-architecting-stage).

Coordinare adapts the contract to the performer protocol, so everything downstream is the
same as for a performer: the verdict cache keyed by head (spec 125), the feedback loop to
the implementer, the system-error retry budget, and resumption after a restart.

The adapter enforces these rules for every provider:

- **One request per card, stage and head.** The request id is derived from the card, the
  stage, the head and an attempt number. Submitting again (a restart, a re-dispatch) finds
  the job that already exists instead of starting another.
- **The verdict belongs to the head that was judged.** When the result arrives, its head is
  compared with the pull request's current head. A result for an older head is refused, and
  the next dispatch judges the current head.
- **Failure is never a pass.** An unreachable provider, a GitHub read that fails, or a job
  that ended without delivering anything is a retryable system error: the card is retried on
  the daemon's usual schedule and blocks with the reason once that budget is spent. A job
  that ended without delivering anything is retried under the next attempt's request id.
- **A finished attempt is never resubmitted automatically.** A job that may already have
  posted its record, or that escalated to a person, blocks the card with the reason, and any
  automatic dispatch finds that job again. Only an operator restart of the stage starts the
  next attempt for the same head, whatever the last attempt said. A restart that has not
  been dispatched yet survives a daemon restart (the override is persisted). What a daemon
  restart forgets is which later attempt a restart already started: the next dispatch finds
  the earlier, finished job again, which is the side that never posts twice.

## Scrutare for the reviewing stage

### Configure

```yaml
stage_providers:
  scrutare:
    kind: scrutare
    url: http://scrutare:8080   # reachable from coordinare
    token_env: SCRUTARE_API_TOKEN       # NAME of the env var holding the token
    installation: 12345678              # GitHub App installation id
    review_login: "vivi-scrutare[bot]"  # the login Scrutare's reviews are posted under

performers:
  reviewer:
    provider: scrutare
```

- `url` is Scrutare's App service as coordinare reaches it. Scrutare's docs keep
  `/api/reviews` and `/api/jobs` off the public route, so this is normally the cluster
  Service.
- `token_env` names the environment variable that holds Scrutare's performer API token
  (`SCRUTARE_APP_API_TOKEN` on the Scrutare side). Put the value in `.env` or the
  deployment's secret store, never in `config.yaml`. A value that is not a variable name is
  refused at load, and the error does not repeat it.
- `installation` must be the installation Scrutare's `SCRUTARE_APP_INSTALLATIONS` lists for
  the repository, or Scrutare refuses the request.
- `review_login` (required) tells coordinare which reviews are Scrutare's. It cannot also be listed in
  `human_reviewers`. Either form of an App's login (`vivi-scrutare[bot]` or `vivi-scrutare`)
  matches.

To opt in one symphony only, leave the global reviewer alone and override it:

```yaml
symphonies:
  - name: widgets
    github_project_number: 12
    overrides:
      performers:
        implementer: {}
        reviewer:
          provider: scrutare
```

`overrides.performers` replaces the global `performers` block for that symphony, so list
every role the symphony runs. The `stage_providers` catalog is read from the global config
only; a symphony that overrides it is refused at load. A provider-backed stage takes no
performer slot while the provider works.

Scrutare runs its own harness, model and configuration (`scrutare.yaml` on the reviewed
repository's default branch), so the reviewer's `backend`, `mode`, `workflow` and tuning
have no effect while it is opted in. The Scrutare repository needs that `scrutare.yaml`, or
every job fails before it starts.

### Check

`coordinare doctor` checks every provider a role uses: `GET /healthz` must answer, and an
authenticated read of `/api/jobs` must reach Scrutare's performer API and accept the token. The same check runs in the startup preflight.
`coordinare config validate` checks the configuration itself: the provider exists, it can
fill the role, the token is referenced by name, and the review login is not a human reviewer.

### Verdicts

| Scrutare | Coordinare |
| --- | --- |
| `approve` | the reviewing stage passes; the verdict is recorded against the reviewed head |
| `changes_requested` | the review's body and inline comments go to the implementer through the existing feedback loop; the next head is reviewed again |
| `escalated` | the card blocks for a person, with a link to Scrutare's review; an operator restart of the reviewing stage starts a fresh Scrutare review |

Scrutare's GitHub review is the reviewing stage's own output. In a symphony whose reviewer
uses Scrutare, `monitor_pr` never routes it a second time as outside feedback, and never
counts it as an approval, even if its login is on `trusted_bot_reviewers` or
`human_reviewers`. Only a human approval merges. A symphony that keeps its performer
reviewer treats Scrutare's reviews like any other bot's.

Feedback queued for the reviewing stage (for example a correction for the reviewer) is not
forwarded: Scrutare judges each head afresh. Coordinare logs
`stage_provider.feedback_not_forwarded` when that happens.

### Failures

| Job ends | What coordinare does |
| --- | --- |
| Scrutare unreachable, 5xx, queue full | retryable system error |
| `failed`, `interrupted` or `not_started` with `delivery: not_sent` | retryable system error; the retry submits the next attempt |
| any end with `delivery: confirmed` | blocks: the review was posted and a later step failed, and Scrutare says not to request another |
| any end with `delivery: uncertain` | blocks: the review may have been posted; inspect `posting.json` in the retained run first |
| `posted` without a receipt bound to the reviewed head, or posted under a login other than `review_login` | blocks: not a pass, and not safe to request again |
| 401 or 403 | at dispatch, blocks as a configuration error (check the token and the installation); while polling, a retryable system error |
| result for a head the pull request has moved past | refused as a retryable error; the current head is reviewed |

### One review per head, and Scrutare's webhook

Scrutare deduplicates performer API requests by request id only. Its docs say performer API
jobs "are never merged or skipped", so a webhook review of a head and coordinare's request
for the same head are two reviews. For a repository whose reviews coordinare drives, keep
Scrutare's App webhook from reviewing it (for example, run the App with its webhook
inactive). The gap is tracked in Scrutare's issue tracker.

### Timing

A review can take as long as Scrutare's `SCRUTARE_APP_JOB_TIMEOUT` (55 minutes by default)
and reports no progress events while it runs. If `dispatcher_dedup.stall_timeout_seconds`
or a reviewer `timeout_seconds` is set, set it above Scrutare's job timeout.

## Progettare for the architecting stage

Progettare plans one GitHub issue: a bounded read-only survey, one blueprint, sizing, and a
brief for each reader (implementer, QA, documenter). It commits nothing and posts nothing.
Coordinare runs its released CLI, `progettare blueprint`, at a pinned version, and turns the
run's briefs into the spec-165 blueprint its readers already take their briefs from.

### Configure

```yaml
stage_providers:
  progettare:
    kind: progettare
    version: "2026.11.2"                          # `progettare --version` must print exactly this
    runs_dir: /var/lib/coordinare/progettare-runs  # survives a coordinare restart
    gh_token_env: GITHUB_TOKEN                    # NAME of the env var with a GitHub token
    pass_env: [OPENAI_API_KEY]                    # NAMES of the model credentials it needs
    # command: progettare                         # a name on PATH or an absolute path
    # config_path: /etc/coordinare/progettare.yaml
    # timeout_seconds: 3600

performers:
  architect:
    provider: progettare
```

- Install Progettare where coordinare runs (its release installer puts the CLI and its own
  Nare runtime in place), with `git` and `gh` on `PATH`. In a container deployment that
  means the coordinare image, not the performer image.
- `command` is a name on `PATH` or an absolute path; a relative path is refused.
- `version` is the release coordinare speaks to. A run refuses to start under another
  version, and `doctor` says which version is installed.
- `gh_token_env` names the variable that holds a GitHub token able to read the issue and
  clone the repository. The run receives it as `GH_TOKEN`; git gets it through the
  environment, never on a command line. A coordinare that signs in as a GitHub App needs a
  token here as well, since the run cannot mint one.
- `pass_env` names the other variables the run receives, typically the model provider's
  credential for the models `progettare.yaml` selects. The run gets nothing else from the
  daemon's environment beyond `PATH`, `HOME` and the locale.
- `config_path` is the `progettare.yaml` to plan with. Without it, each run uses the one on
  the planned repository's default branch, and a repository without one blocks the card.
- A symphony opts in through `overrides.performers`, as for Scrutare. Progettare pins its
  own harness and models, so the architect's `backend`, `mode`, `workflow` and tuning have no
  effect while it is set.

### Check

`coordinare doctor`, and the same check in the startup preflight, confirms the CLI is
installed at the pinned version, `git` and `gh` are on `PATH`, `gh_token_env` and every
`pass_env` variable are set, `config_path` exists when given, and `runs_dir` is writable.

### How a run is found again

Progettare has no job API and takes no request id (ViviDynamics/progettare#69), so
coordinare names each run itself. Every request gets its own directory under `runs_dir`,
named by the request id, which is derived from the card, the stage, the pin and the
attempt. A detached runner clones the default branch there, runs the CLI with that
directory's `runs/` as `--runs-dir`, and holds a lock on the directory for as long as the
runner or Progettare lives. Looking a request up reads its directory:

- the lock is held: the run is in flight, so a restart or a re-dispatch waits for it;
- `run.json` says `complete`: the plan;
- `run.json` says `blocked`: the questions for a person;
- `run.json` says `failed`, or there is no `run.json` (the clone failed, the run timed out,
  or the host restarted and killed it): ended without a plan.

A restart of coordinare leaves the runner running, and the next poll finds it by its lock,
so a restart during planning never produces a second plan. A restart of the host or the
container kills the run; that attempt ended without a plan and is retried, which still
leaves one plan.

### Outcomes

| Progettare | Coordinare |
| --- | --- |
| `complete` | the architecting stage passes (`plan_committed`) with the blueprint: the implementer brief's milestones, the QA brief's testable criteria, the documenter brief's topics (when Progettare's sizing turned the documenter on; Progettare names no location, so the documenter workflow writes each topic on a page under the docs root named by the topic), and the size (`single_turn` is small, `milestone_loop` is large) |
| `blocked` | the card blocks with Progettare's questions |
| `failed`, clone failure, timeout, a run killed by a restart | retryable system error; the retry starts the next attempt, which is safe because the CLI posts nothing |
| a record at another contract version | blocks: coordinare speaks Progettare's artifact contract version 1 and never guesses at another |
| no `progettare.yaml` | blocks with the reason |
| token not set, or another version installed | blocks at dispatch as a configuration error |

Nothing is committed to the branch. As with the spec-165 workflow architect, the blueprint
is held on the card's session and each reader's brief is cut from it at dispatch. The run
directory keeps Progettare's full record (`blueprint.json`, the survey, the briefs) for
people to read; the blueprint's `source.run_dir` points at it.

A finished plan must be of the issue as it is now: when the run ends, the title and body it
recorded are compared with the issue's current ones, and a plan of text that changed since
is refused and the current text planned. The blueprint also records the pin it answers, so
a later dispatch of the architect reuses it only while the card's title, body and recorded
answers still give that pin. An answer recorded while a run is in flight is not seen by
that run (Progettare read the issue's comments when it started); the plan is used, and the
next dispatch of the architect plans again.

Only issues on the configured GitHub host (github.com, or the host of `github_api_url` for
GitHub Enterprise) are planned. The clone sends the GitHub token, so a card whose issue URL
names another host blocks instead.

Feedback routed back to the architect, such as security findings or an observer's
correction, cannot reach Progettare: it reads the issue, not coordinare's feedback. As for
Scrutare, coordinare logs `stage_provider.feedback_not_forwarded`, and the plan of the
unchanged issue stands; it is not planned a second time. If the same findings come back,
coordinare's feedback-cycle budget blocks the card. To make Progettare plan for a finding,
put what the plan must address in the issue (which is a new request), or restart the
architecting stage.

### Answering a blocked run

A new answer starts a new run. When a person answers the card's questions, coordinare
records the answer, and the recorded answers are part of the pin, so the next dispatch is a
new request. Editing the issue body changes the pin too. Progettare reads the acceptance
criteria from the issue body, so a question about them is answered by editing the body's
acceptance section; Progettare reads other answers from the issue's comments. An operator
restart of the architecting stage also starts a new run.

### Timing

A run can take as long as the clone (up to 10 minutes) plus `timeout_seconds` (an hour by
default), and reports no progress while it works. If the architect's `timeout_seconds` or
`dispatcher_dedup.stall_timeout_seconds` is set, set it above Progettare's `timeout_seconds`
plus 600.

### Work items that are not GitHub issues

Progettare plans GitHub issues only. A card whose work item is not one (no GitHub issue URL)
blocks at dispatch with a reason that names ViviDynamics/progettare#68, which tracks
planning a work item handed in as data. Until it lands, keep the architect on a performer
for such a symphony.

