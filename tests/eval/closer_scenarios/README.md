# Closer scenario eval (spec 172)

Five fixture pull requests run through the closer workflow against a fake GitHub:

| fixture | threads | expected outcome | what it exercises |
| --- | --- | --- | --- |
| `clean` | all resolved | approved, zero model calls, nothing resolved | SC-002, the common path |
| `answered` | a reply explains the fix | approved, one model call, the thread resolved with its quote | the only judgement the stage makes |
| `open` | one comment, no reply | changes requested, zero model calls, nothing resolved | classification by rule |
| `outdated` | unresolved but the code moved | approved, zero model calls, resolved by rule | the outdated rule |
| `hallucinated_quote` | a bare "Thanks!" reply, the model claims a fix | changes requested, the judgement discarded | the quote gate |

Scoring checks the verdict, the model call count, that no approval carries an open thread, that every resolution is justified by the outdated rule or a quote in that thread, that exactly one COMMENT review was posted, and that a rejection resolves nothing.

Stubbed (deterministic, runs under pytest):

```bash
PYTHONPATH=src:agent/performer/src .venv/bin/pytest tests/eval/closer_scenarios -q
PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.closer_scenarios
```

Live (real model through the LiteLLM gateway, still a fake GitHub: no real thread is fetched or resolved):

```bash
set -a && source .env && set +a
docker run --rm --network host --entrypoint python \
  -e PYTHONPATH=/work/src:/work/agent/performer/src:/work \
  -e LITELLM_BASE_URL -e LITELLM_MASTER_KEY -e COORDINARE_INFERENCE_MODEL \
  -v "$PWD":/work:ro -w /work coordinare-performer:full \
  -m coordinare.eval.closer_scenarios --live --only answered
```
