# Quickstart: Architect Role Workflow

## Enable (per role, default off)

```yaml
performers:
  architect:
    backend: codex
    mode: single-glm53flash-spark
    workflow: architect            # NEW: bounded intake, survey, blueprint, size, report
    workflow_env:                  # optional
      ARCHITECT_SURVEY_MAX_COMMANDS: "12"
      ARCHITECT_SURVEY_MAX_OUTPUT_CHARS: "4000"
```

Nothing else changes in config. With the key absent the architect writes `plan.md` and `tasks.md` exactly as before.

## What you will see

- The architect performer's log carries `workflow.start`, `architect.survey` (one line per command with `allowed`/`refused`), `workflow.model_call` timings, and `workflow.completed` with `step_durations_ms`.
- The card's branch has no commit from the architect stage.
- The implementer's dispatch carries `implementation_brief` (and `implementer_single_turn: true` for a small card); the documenter side run, when dispatched, carries `documentation_brief`; QA carries `verification_brief`.
- coordinare.log: `blueprint.lifted card_id=... size=... milestones=N docs=N`, `documenting_side.dispatched` / `.completed` / `.failed`, `dispatch_performer.single_turn card_id=...`.

## Run the eval

```bash
.venv/bin/python -m coordinare.eval.architect_scenarios            # stubbed model, deterministic, also runs under pytest
.venv/bin/python -m coordinare.eval.architect_scenarios --live     # real gateway, minutes, not a CI gate
```

Fixtures: `trivial` (one-line copy fix: expect small, no docs), `feature` (mid-size: expect large, 2 to 4 milestones, 1 doc topic), `schema` (tables plus interfaces: expect large, data_model changes, interfaces, docs).

## Verify the push path (applies to every role)

A performer's push now runs `git fetch origin <branch>` and `git rebase origin/<branch>` first when the remote branch exists, then pushes without `--force`. `--force` is used only when the remote branch does not exist yet. A rebase conflict fails the performer with `push_branch.rebase_conflict` in its log and a named reason in its response.
