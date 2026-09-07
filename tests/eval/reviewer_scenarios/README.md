# Reviewer scenario eval (spec 169)

Six fixture pull requests run through the reviewer workflow:

| fixture | diff | expected outcome | what it exercises |
| --- | --- | --- | --- |
| `clean` | two files, nothing wrong | approved, one COMMENT review | verdict by code, coverage without a pass |
| `findings` | unguarded division | changes requested, one inline comment | anchor check, REQUEST_CHANGES |
| `hallucinated_anchor` | model anchors at line 99 with invented evidence | dropped, re-anchored once, changes requested | FR-006 drop and the single re-anchor call |
| `prior_feedback` | one comment fixed, one without a disposition | changes requested with `unaddressed_feedback` | FR-007, body-anchored finding |
| `truncated` | diff cut through a third file | coverage pass opens it, approved | FR-004, FR-009 |
| `docs_by_implementer` | brief present, `docs/usage.md` edited | changes requested with `documentation_by_implementer` | FR-008 |

Scoring checks the verdict, that every finding is anchored to a changed file, the rule categories, the single recorded review and its event, the coverage pass, and the executed write-free check. The eval never posts to GitHub: a recording poster stands in for the Reviews API in both modes.

Stubbed (deterministic, runs under pytest):

```bash
PYTHONPATH=src:agent/performer/src .venv/bin/pytest tests/eval/reviewer_scenarios -q
PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.reviewer_scenarios
```

Live (real model through the LiteLLM gateway; minutes; a rate to read, not a CI gate):

```bash
set -a && source .env && set +a
docker run --rm --network host --entrypoint python \
  -e PYTHONPATH=/work/src:/work/agent/performer/src:/work \
  -e LITELLM_BASE_URL -e LITELLM_MASTER_KEY -e COORDINARE_INFERENCE_MODEL \
  -v "$PWD":/work:ro -w /work coordinare-performer:full \
  -m coordinare.eval.reviewer_scenarios --live --only findings
```
