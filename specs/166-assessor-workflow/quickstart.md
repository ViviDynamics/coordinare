# Quickstart: Assessor Role Workflow (spec 166)

## Enable

On the assessor role in the (gitignored) `config.yaml`, beside the backend and model it already has:

```yaml
    workflow: assessor
```

With the key set, the assessing stage runs intake, assess, gate, report: one model call, no commands, no commits. Without it the assessor is byte for byte what it is today, including the committed `docs/cards/<n>/assessment.md`. Rebuild the performer images (base, full, extra) before enabling; the workflow code ships in the image.

## Stubbed eval (deterministic, runs in CI)

```bash
PYTHONPATH=src:agent/performer/src .venv/bin/pytest tests/eval/assessor_scenarios tests/unit/workflows/assessor -q
PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.assessor_scenarios
```

Fixtures: `clear` (ready, no questions, `criteria_source` card), `ambiguous` (not ready, at most two outcome questions, report `blocked`), `answered` (two answered rounds carried in, must return ready with the rest as assumptions). Scoring checks readiness, question count, the no-re-ask rule, the criteria source, the write-free record, and that the assessment reaches an architecting dispatch and no other.

## Live eval (real model through the gateway, a rate to read, not a gate)

The coordinare package imports `gql`, which the performer image lacks, so live rounds run inside the `full` image with the worktree mounted read only and the results scored on the host, as spec 165 did:

```bash
set -a && source .env && set +a
docker run --rm --network host --entrypoint python \
  -e PYTHONPATH=/work/src:/work/agent/performer/src:/work \
  -e LITELLM_BASE_URL -e LITELLM_MASTER_KEY -e COORDINARE_INFERENCE_MODEL \
  -v "$PWD":/work:ro -w /work coordinare-performer:full \
  -m coordinare.eval.assessor_scenarios --live --only clear
```

Record each fixture's round time against SC-001 (under five minutes p90 including container start).

## Verify the three properties by hand

- **No commits (FR-003, SC-002).** After a live assessor round the card branch has no commit from the assessor and no `docs/cards/<n>/assessment.md`. The report's `write_free_check` records the executed `git status --porcelain`.
- **Bounded rounds (FR-006 to FR-009, SC-003, SC-004).** `tests/unit/workflows/assessor/test_gate.py` pins each rule; the mutation checks are recorded in the PR description. On a live card, the issue never shows a question that repeats an answered one, and the card is never blocked for clarification a third time.
- **Hand-off (FR-012 to FR-014, SC-005).** After `assessment_complete`, the card's entry in `coordinare.state.json` carries `assessment`; the next architecting dispatch payload carries it and no other dispatch does (`tests/contract/test_dispatch_payload.py`); the architect container log shows the intake opening with the assessment section.
