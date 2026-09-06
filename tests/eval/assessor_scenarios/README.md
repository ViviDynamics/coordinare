# Assessor scenario eval (spec 166)

Three fixture cards run through the assessor workflow:

| fixture | card | expected outcome | expected criteria source |
| --- | --- | --- | --- |
| `clear` | well-written card with criteria, typo fix | ready, no questions | card |
| `ambiguous` | vague card, no criteria, no clarifications | blocked, 1-2 questions | assessor |
| `answered` | no criteria, 2 answered clarifications, 1 new question asked | ready, questions as assumptions | assessor |

Each fixture exercises specific gate rules: `clear` passes through ready; `ambiguous` caps questions and returns blocked; `answered` forces ready after 2 answered rounds and carries the remaining question as an assumption.

Scoring checks readiness, question count, criteria source, no re-asked questions (FR-007), write-free record (no commands ran), and the assessment hand-off to architecting (FR-013).

Stubbed (deterministic, runs under pytest):

```bash
.venv/bin/pytest tests/eval/assessor_scenarios -q
.venv/bin/python -m coordinare.eval.assessor_scenarios
```

Live (real model through the LiteLLM gateway; minutes; a rate to read, not a CI gate):

```bash
set -a && source .env && set +a
docker run --rm --network host --entrypoint python \
  -e PYTHONPATH=/work/src:/work/agent/performer/src:/work \
  -e LITELLM_BASE_URL -e LITELLM_MASTER_KEY -e COORDINARE_INFERENCE_MODEL \
  -v "$PWD":/work:ro -w /work coordinare-performer:full \
  -m coordinare.eval.assessor_scenarios --live --only clear
```

Record each fixture's round time against SC-001 (under five minutes p90 including container start).
