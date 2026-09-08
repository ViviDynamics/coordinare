# Quickstart

## What this changes for an operator

Nothing, until you switch a role on. Both are disabled by default, and the
running configuration sets neither. When the advocate is enabled it behaves as
it did before with one visible difference: an answer it cannot ground in a
document it read becomes an escalation instead of a reply.

## Turning the advocate on

```yaml
advocate:
  enabled: true
  github_repo: your-repo
  doc_sources: [README.md, docs/quickstart.md]
  confidence_threshold: 0.70
  handled_label: advocate-handled
  escalation_label: needs-human
  scan_interval_seconds: 900
```

The role runs in a performer. It reads `doc_sources` from the working copy
rather than fetching each file over the API, so a path that does not exist in
the repository is simply recorded as unread. If you point `doc_branch` at a
branch other than the default, that branch is fetched explicitly.

## Turning the curator on

```yaml
curator:
  enabled: true
  github_repo: your-repo
  label: curator-proposed
  backlog_column: Backlog
  max_per_run: 5
  scan_interval_seconds: 3600
```

`backlog_column` must not be the column coordinare dispatches from; the
configuration refuses to load if it is. A promoted issue lands there with the
label and a comment saying why it qualified, and a human moves it on.

## Watching a run

Both roles log the same shape as every other workflow: one event per step with
its duration, one per model call with its elapsed time and completion tokens,
and a final event carrying the verdict.

```
advocate.intake      issues=12 documents_read=2
advocate.triage      escalated=1 reason=sensitive_keyword calls=0
advocate.classify    sent=11 returned=11
advocate.gate        replied=7 withheld=2 escalated=2
advocate.act         labelled=11 commented=9
```

A `withheld` count above zero is the interesting line: the model produced an
answer and the gate refused it because it cited a document the run never read.
The record names the issue and the reason.

## Verifying it did not write

Every run carries the executed output of `git status --porcelain` on its record.
It is empty. A run that commits, pushes or opens a pull request is a defect, and
the tests assert against exactly that, because the performer's status handling
ends in a path that would do all three for any role that failed to return first.

## Running the tests

The two trees must run separately.

```bash
PYTHONPATH=src:agent/performer/src .venv/bin/pytest tests -q --cov=coordinare --cov-fail-under=90
```

```bash
PYTHONPATH=src:agent/performer/src .venv/bin/pytest agent/performer/tests -q
```

The eval fixtures run stubbed in CI and live through the gateway:

```bash
PYTHONPATH=src:agent/performer/src .venv/bin/pytest tests/eval/advocate_scenarios tests/eval/curator_scenarios -q
```

## What to check first when something looks wrong

- **A run never finishes.** Its terminal status is missing from the closed status
  literal, so the poll loop never breaks.
- **A run opened a pull request.** Its branch in the status handler is missing or
  sits after the shared tail.
- **The role stopped running entirely.** The in-flight marker is stuck. It is not
  persisted, so a restart clears it; if it recurs, the marker is being set after
  the dispatch rather than before.
- **An outcome vanished after a restart.** The completion handler did not call
  the snapshot flush, and the gated save deferred it.
- **A duplicate reply after a restart.** Something is deciding "already handled"
  from memory instead of from the issue's labels.
