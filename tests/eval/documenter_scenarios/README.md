# Documenter scenario eval (spec 171)

Six fixtures, each on a real temporary git repository with a small wiki:

| fixture | change | expected outcome | what it exercises |
| --- | --- | --- | --- |
| `trivial` | a version bump nothing cites | empty plan, no model call, no commit | SC-005 |
| `feature` | a payments module, brief names one page | brief page, cited architecture page, generated README, pointers, one commit | plan by code, README shape, one commit |
| `shape` | same, model adds a changelog heading | page dropped naming the check | the page contract |
| `hallucinated_citation` | same, model cites a missing module | page dropped with the citation named | SC-001 |
| `init` | a repository without a wiki, six packages | skeleton capped at 8, deferred packages, pointers added | init mode |
| `pointers` | stale pointer section in AGENTS.md | section replaced between markers, rest byte for byte | FR-011 |

Scoring checks the verdict, the written count, the model call count, exactly one commit touching only documentation paths, a clean tree, the dropped pages, the README shape, and that every committed page cites only paths in the tree.

Stubbed (deterministic, runs under pytest):

```bash
PYTHONPATH=src:agent/performer/src .venv/bin/pytest tests/eval/documenter_scenarios -q
PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.documenter_scenarios
```

Live (real model through the LiteLLM gateway; commits land only in the temporary repository, nothing is pushed):

```bash
set -a && source .env && set +a
docker run --rm --network host --entrypoint python \
  -e PYTHONPATH=/work/src:/work/agent/performer/src:/work \
  -e LITELLM_BASE_URL -e LITELLM_MASTER_KEY -e COORDINARE_INFERENCE_MODEL \
  -v "$PWD":/work:ro -w /work coordinare-performer:full \
  -m coordinare.eval.documenter_scenarios --live --only feature
```
