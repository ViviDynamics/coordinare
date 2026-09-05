# Quickstart: Spec 164

## Turning it on

The layer is default-off. A role uses today's path until a workflow is named:

```yaml
roles:
  qa:
    backend: claude_code      # still used, as a step primitive
    workflow: qa              # NEW — opt in
    workflow_env:             # NEW — how the app under test boots (optional)
      PORT: "3000"
      QA_APP_START_COMMAND: "bin/rails s -b 127.0.0.1 -p 3000 -e test"
      QA_APP_SEED_COMMAND: "bin/rails db:migrate db:seed"   # runs BEFORE start
      QA_APP_BOOT_TIMEOUT: "180"                             # seconds; default 60
```

Removing the `workflow:` line restores the current behaviour exactly. That is the
rollback, and it needs no code change.

### How the app boots

The workflow needs the app under test serving before it can drive a flow or
compare rendered pages. Its environment is layered, lowest to highest:

1. the performer process env
2. the env cache's activation delta (`activate.sh`) — `PORT`, the toolchain
   `PATH`, `POSTGRESQL_*` / `REDIS_*`; this is the same env the legacy QA
   capture path uses
3. `workflow_env` from the role config — the operator's explicit intent, and
   it wins

`QA_APP_START_COMMAND` matters when `infer_app_start_command` cannot recognise
the project. Inference is deliberately conservative (Rails, Django, Node; `None`
otherwise), and a wrong guess wastes the boot window, so anything else needs
the command stated. `QA_APP_SEED_COMMAND` runs before the server starts: a
migrate or seed against an already-running app is a different, usually wrong,
operation.

`QA_APP_BOOT_TIMEOUT` defaults to 60s. That is too short for a Rails app that
migrates on startup and needlessly long for a stub that answers in 200ms; set
it for the app you have. A malformed value logs a warning and uses the default
rather than failing the run.

If `PORT` is already answering when QA starts, the workflow **adopts** that
server rather than launching its own, logs `qa.boot.adopted_existing_server`,
and sets `workflow_metrics.adopted_existing_server`. Inside a Docker or k8s
performer that is harmless (loopback is per container). On the subprocess
transport it means another process owns the port — check the metric before
trusting a verdict.

An **empty plan** — the model could derive no check for the stated criteria —
is a **fail** with one `unmet_criterion` finding per criterion, not an
`environment_error`. "Nothing to check" on a card that has criteria means the
criteria were not demonstrated. Only a card with no criteria at all yields
"could not verify".

The **base** app (the merge-base commit, for the before/after comparison) boots
from a git worktree with the same env and start command on its own free port.
If it never answers, the run reports `environment_error` — with no baseline the
delta is unknown, and "nothing regressed" cannot honestly be claimed.

## Running the deterministic tests

These use recorded model responses and never touch the gateway:

```bash
.venv/bin/pytest tests/unit/workflows -q
```

## Running the scenario eval

Not a CI gate. Calls a real model, takes minutes, reports a rate:

```bash
.venv/bin/python -m coordinare.eval.qa_scenarios --repeats 5
```

Output is a pass rate per scenario:

```text
healthy         5/5   verdict=pass
regression      5/5   verdict=fail        named: password field
misplaced       4/5   verdict=fail        named: wrong module (1 miss)
incomplete      5/5   verdict=fail
cosmetic_noop   3/5   verdict=fail        (2 false passes)
env_broken      5/5   environment_error
```

A scenario dropping below its recorded rate is the signal to investigate. There
is no threshold that fails a build — see the Constitution Check in `plan.md` for
why that boundary exists, and do not wire this into CI.

## Adding a scenario

Add a manifest under `tests/eval/qa_scenarios/fixtures/<name>.yaml` with
`base_files`, `head_files`, `criteria`, `expected_verdict` and `must_name`. The
repository is generated at run time; do not commit a `.git` directory.

## Debugging a workflow run

Step transitions arrive as `BackendEvent`s, so the dashboard activity feed shows
progress live. For a finished run, `WorkflowMetrics` on the result carries
per-step durations, the model-call count, and how many truncation retries and
schema reprompts were needed — the three numbers that explain most bad runs.
