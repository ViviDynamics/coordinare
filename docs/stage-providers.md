# Stage providers

A stage provider is an outside service that fills one lifecycle stage in place of a
performer. Coordinare hands it a pull request and a head, and takes back a verdict. The
providers are [Scrutare](https://github.com/ViviDynamics/scrutare) for the reviewing stage
(issue #402) and [QAre](https://github.com/ViviDynamics/qare) for the QA stage (#404).
Progettare (architect, #403) is meant to use the same contract.

Nothing changes until a role names a provider. A config without `stage_providers`, or
whose roles name none, runs every stage on performers exactly as before.

## The stage contract

Every provider fills the same contract (`src/coordinare/services/stage_contract.py`):

| | |
| --- | --- |
| Input | the repository, the pull request, and the head SHA to judge; for QA also the base, the work item, and its acceptance criteria, each with an id that is stable across heads |
| Output | a verdict (`pass`, `changes_requested` or `needs_human`), the findings, the head that was actually judged, and a reference to the record; for QA also the outcome of every criterion with its evidence |

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
- **A QA pass needs every criterion proven.** A pass that names no criterion, or leaves one
  of them failed or unverified, holds the card for a person (the spec 120 evidence floor).
- **Evidence is posted only for the current head.** A provider that leaves posting to
  coordinare (QAre) is asked to post its record after the head check, never before.

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

## QAre for the QA stage

QAre is a CLI, not a service. Coordinare runs the pinned QAre on its own host against a
checkout of the pull request, with QAre's posting turned off, and posts the evidence itself
where the built-in QA posts its own.

### Configure

```yaml
stage_providers:
  qare:
    kind: qare
    version: "2026.11.42"            # the exact release `qare --version` must print
    command: ["qare"]                # how to start it (default), as an argument list
    profile: .qa                     # the QA profile's directory in the repository (default)
    env_passthrough: [OPENAI_API_KEY]  # NAMES of variables QAre's nare may read
    # work_dir: /var/lib/coordinare/qare  # default: stage-providers/<name> beside the state file
    # timeout_seconds: 3600
    # retain_days: 14                 # how long a finished run's evidence and logs are kept

performers:
  qa:
    provider: qare
```

- Install QAre where coordinare runs: the host install from QAre's docs (the `@qare/cli`
  package at the pinned version with its pinned nare), plus Docker for a profile that boots
  an app. `version` must match exactly, because QAre's interfaces can change between
  releases; a run on any other version holds the card.
- QAre needs no token from coordinare. Its nare reads its model endpoint and key from the
  environment, and only the variables named in `env_passthrough` reach it, along with a
  short list of basics (`PATH`, `HOME`, the locale, the Docker variables). Coordinare's own
  secrets never do. A value written where a name belongs is refused at load without being
  repeated.
- The repository needs a QAre profile (`qare init` writes one). The base revision's profile
  is used when it has one, so the profile that runs is one the pull request cannot change,
  as in QAre's own pipeline; a pull request that adds the first profile uses its own. With
  no profile on either side the card is held with a message that says to run `qare init`.
  It does not fall back to the built-in QA: the symphony chose QAre, and a silent fallback
  would hide that its repository is not set up.
- QAre pins its own harness, so the qa role's `backend`, `mode`, `workflow` and tuning have
  no effect while it is opted in.

### Running QAre in its own container

QAre runs the pull request's code: its checks, and the app it boots. A host install runs
those where coordinare runs. To keep them off that host, run QAre from its published image at
the same pinned release. The runner hands QAre absolute paths under `work_dir`, so mount
`work_dir` at the same path, run as a user that can write it, and forward the variables QAre
needs with `-e`:

```yaml
stage_providers:
  qare:
    kind: qare
    version: "2026.11.42"
    work_dir: /var/lib/coordinare/qare
    command: [docker, run, --rm, --user, "1000:1000",
              -v, "/var/lib/coordinare/qare:/var/lib/coordinare/qare",
              -e, OPENAI_API_KEY,
              "ghcr.io/vividynamics/qare-web:2026.11.42"]
    env_passthrough: [OPENAI_API_KEY]
```

The image's entry point is `qare`, so the runner's `--version`, `plan` and `run` reach it
unchanged. A profile that boots an app needs a container runtime inside that container too;
how much of the host to hand it (for example its Docker socket) is the operator's trust
decision, as it is for coordinare's own containers (`docs/security/threat-model.md`). The
checkout itself (step 2 below) is made by the runner, not by QAre.

Opt one symphony in with `overrides.performers`, as for Scrutare.

### What a run does

Each run has a directory of its own under `work_dir`, named by its request id (card, stage,
head and attempt). A detached process (`python -m coordinare.services.qare_runner`) does the
work, so a daemon restart finds the run still going or finished:

1. `qare --version` must print the pinned version.
2. The head and base are fetched into the run's directory with coordinare's GitHub token,
   which is never written to disk and never reaches QAre.
3. The work item's criteria are written as `{id, text}`. They come from the architect's
   verification brief when it has criteria, else from the issue's acceptance criteria, and
   each id is a hash of its text, so a criterion keeps its id on every head.
4. `qare plan --criteria` maps each criterion to checks against the change's diff. A QAre
   job whose criteria carry no checks runs nothing (every criterion is unverified), so the
   plan comes first (ViviDynamics/qare#450 asks QAre's docs to say so).
5. `qare run --plan` runs the checks against both sides and writes
   `evidence/result.json`. Its `schemaVersion` is checked before anything else is read.

A step that runs past `timeout_seconds` is stopped with everything it started (each command
runs in a process group of its own). When a run ends its checkouts are removed, and a
finished run's directory, with its evidence and logs, is removed `retain_days` after it
ended. A run still going is never removed. QA needs the pull request's base: while GitHub
gives none, the dispatch is retried rather than run against the head alone.

### Verdicts

| QAre | Coordinare |
| --- | --- |
| `passed` (exit 0), every criterion proven | QA passes for the tested head (`qa_passed`, recorded in the verdict cache) |
| `failed` (exit 1) | the failing criteria, with their reasons and evidence links, go to the implementer (`qa_failed`); the next head is run again |
| `blocked` (exit 2) | the card is held: the environment stopped the run, not a defect; the reasons are in the comment |
| `refused` (exit 3) | the card is held: QAre refused for a named reason (a missing stub, a missing profile) |
| `waived` (exit 5) | the card is held: a person recorded a waiver in QAre, and a waiver is never a pass, so a person decides whether the card moves on |

### Evidence

The run's `result.json`, `plan.json` and every evidence file the result names are committed
to the repository's `qa-assets` branch under `qare/<request id>/`, the branch the built-in
QA keeps its screenshots on. Text is redacted again before it leaves the host; files outside
the evidence directory, symbolic links and binaries other than images are left out. One
"QA Evidence (QAre)" comment, naming the head that was tested, every criterion's outcome and
links to its evidence, goes on the pull request and on the issue. A held run gets the same
comment when it is held on its verdict (blocked, refused, waived, or a pass that is not
proven for every criterion), so the person it is held for can read why.

Publishing can be repeated safely: evidence already on the branch is not committed twice, a
comment carrying the run's marker is not posted twice, and `published.json` in the run's
directory records what is done. A publish GitHub refuses is a retryable system error.

### Failures

| Run ends | What coordinare does |
| --- | --- |
| QAre cannot start, the checkout or the plan fails, the run times out, the runner stops without a status | retryable system error; the retry runs the next attempt |
| exit 4 (no verdict), or no `result.json` | retryable system error |
| another version installed than `version` | the card is held: install the pinned release or change the pin |
| an unknown `schemaVersion` or verdict, an exit code that contradicts the verdict, the result of another job | the card is held: the result is outside QAre's contract |
| no profile, or no acceptance criteria | the card is held with what to add |
| result for a head the pull request has moved past | refused as a retryable error and nothing is posted; the current head is run |

### Check

`coordinare doctor` and the startup preflight check that `qare --version` prints the pinned
version, that `work_dir` is writable, and that the configured repository (`github_org` and
`project_name`) has the profile on its default branch.
