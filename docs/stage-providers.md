# Stage providers

A stage provider is an outside service that fills one lifecycle stage in place of a
performer. Coordinare hands it a pull request and a head, and takes back a verdict. The
first provider is [Scrutare](https://github.com/ViviDynamics/scrutare) for the reviewing
stage (issue #402). Progettare (architect, #403) and QAre (QA, #404) are meant to use the
same contract.

Nothing changes until a role names a provider. A config without `stage_providers`, or
whose roles name none, runs every stage on performers exactly as before.

## The stage contract

Every provider fills the same contract (`src/coordinare/services/stage_contract.py`):

| | |
| --- | --- |
| Input | the repository, the pull request, and the head SHA to judge |
| Output | a verdict (`pass`, `changes_requested` or `needs_human`), the findings, the head that was actually judged, and a reference to the record |

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
- **A possible delivery is never resubmitted.** A job that may already have posted its
  record blocks the card with the reason, for a person to check.

## Scrutare for the reviewing stage

### Configure

```yaml
stage_providers:
  scrutare:
    kind: scrutare
    url: http://scrutare.scrutare.svc.cluster.local   # reachable from coordinare
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
- `review_login` tells coordinare which reviews are Scrutare's. It cannot also be listed in
  `human_reviewers`.

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
every role the symphony runs. The `stage_providers` catalog is read from the global config.

Scrutare runs its own harness, model and configuration (`scrutare.yaml` on the reviewed
repository's default branch), so the reviewer's `backend`, `mode`, `workflow` and tuning
have no effect while it is opted in. The Scrutare repository needs that `scrutare.yaml`, or
every job fails before it starts.

### Check

`coordinare doctor` checks every provider a role uses: `GET /healthz` must answer, and an
authenticated read must accept the token. The same check runs in the startup preflight.
`coordinare config validate` checks the configuration itself: the provider exists, it can
fill the role, the token is referenced by name, and the review login is not a human reviewer.

### Verdicts

| Scrutare | Coordinare |
| --- | --- |
| `approve` | the reviewing stage passes; the verdict is recorded against the reviewed head |
| `changes_requested` | the review's body and inline comments go to the implementer through the existing feedback loop; the next head is reviewed again |
| `escalated` | the card blocks for a person, with a link to Scrutare's review |

Scrutare's GitHub review is the reviewing stage's own output. `monitor_pr` never routes it a
second time as outside feedback, and never counts it as an approval, even if its login is on
`trusted_bot_reviewers` or `human_reviewers`. Only a human approval merges.

### Failures

| Job ends | What coordinare does |
| --- | --- |
| Scrutare unreachable, 5xx, queue full | retryable system error |
| `failed`, `interrupted` or `not_started` with `delivery: not_sent` | retryable system error; the retry submits the next attempt |
| `failed` with `delivery: confirmed` | blocks: the review was posted and a later step failed, and Scrutare says not to request another |
| `interrupted` with `delivery: uncertain` | blocks: the review may have been posted; inspect `posting.json` in the retained run first |
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
