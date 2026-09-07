# Quickstart: Closer Workflow

## Enable

```yaml
performers:
  closer:
    workflow: closer
```

Remove the line to restore the prose path exactly.

## What it does

Fetches the PR's review threads, classifies each by rule (resolved, stale because the code moved under it, answered in a reply, or open), asks the model only about the answered ones, checks every judgement against the thread's own comments, derives the verdict, posts one review, then resolves the stale and addressed threads. A card whose threads are all resolved makes no model call.

## Run the stubbed eval

```bash
PYTHONPATH=src:agent/performer/src .venv/bin/pytest tests/eval/closer_scenarios -q
PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.closer_scenarios
```

Five fixtures: `clean` (all resolved, no model call, approved), `answered` (a reply resolves it, quote recorded), `open` (no reply, changes requested, nothing resolved), `outdated` (resolved by rule, no model call), `hallucinated_quote` (the model quotes text no comment contains: discarded, thread stays open, changes requested).

## Run live inside the performer image

```bash
set -a && source .env && set +a
docker run --rm --network host --entrypoint python \
  -e PYTHONPATH=/work/src:/work/agent/performer/src:/work \
  -e LITELLM_BASE_URL -e LITELLM_MASTER_KEY -e COORDINARE_INFERENCE_MODEL \
  -v "$PWD":/work:ro -w /work coordinare-performer:full \
  -m coordinare.eval.closer_scenarios --live --only answered
```

Live mode uses the gateway model with a fake GitHub: no thread is fetched from or resolved on a real pull request.

## Verification checklist

- [ ] Five fixtures pass in CI; `clean` and `answered` pass live.
- [ ] Every rule test fails under its named mutation.
- [ ] `clean` and `outdated` make zero model calls.
- [ ] Without the flag, the closing_review path is unchanged byte for byte.
