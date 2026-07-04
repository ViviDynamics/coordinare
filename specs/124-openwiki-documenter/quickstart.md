# Quickstart: OpenWiki Documenter Backend & Symphony Wiki Bootstrap

> **⚠️ PIVOTED (2026-07 — spec 124).** This document describes the original
> approach: adopting LangChain's **OpenWiki** CLI as a documenter backend. That
> approach was **dropped** (OpenWiki's agentic tool-calling was unreliable across
> every self-hosted model, and cloud models are off-limits for this role).
> The shipped design instead **enhances our own `tech_writer` documenter** to
> maintain a living `docs/wiki/` via the reliable `{files}` JSON contract. See
> **`spec.md`** (the source of truth) and `docs/operators/wiki-documenter.md`.
> Sections below referring to an `openwiki` backend, `openwiki/` output paths,
> or model benchmarking are **historical**.

Operator-facing walkthrough. Assumes the branch `124-openwiki-documenter` is built and images rebuilt.

## 0. Prerequisites
- `set -a && source .env && set +a` before launching coordinare (config `${VAR}` expansion).
- Rebuild performer images **base → full → extra** (Python source is COPYed at build; QA/docs run on layered images).

## 1. Build the image with OpenWiki
`Dockerfile.full` adds `openwiki` to the global npm install (Node 22 already in `:base`). Rebuild:
```bash
# from repo root (build context = repo root)
docker build -f agent/performer/Dockerfile.base -t coordinare-performer:base .
docker build -f agent/performer/Dockerfile.full  -t coordinare-performer:full  .
docker build -f agent/performer/Dockerfile.extra -t coordinare-performer:extra .
# sanity
docker run --rm coordinare-performer:full openwiki --help
```

## 2. Prove gateway passthrough (POC gate 1 — the #1 risk)
```bash
docker run --rm -e OPENAI_BASE_URL=https://litellm.vividynamics.com/v1 \
  -e OPENAI_API_KEY="$LITELLM_MASTER_KEY" -e OPENWIKI_MODEL_ID=spark/gpt-oss:120b \
  -v "$PWD":/repo -w /repo coordinare-performer:full \
  openwiki -p "Summarize this repo's architecture in openwiki/." --print
```
- Reaches LiteLLM & produces `openwiki/` → gate 1 PASS.
- Ignores base_url / hits the wrong host → apply the provider-resolution patch (research R2), rebuild, re-run.

## 3. Run the cross-model benchmark
```bash
scripts/persona_bench.py \
  --repo https://github.com/ViviDynamics/<website-bench>.git \
  --prs tmp/bench_prs.json --config config.yaml \
  --roles documenter \
  --backends openwiki-ephemeral \
  --models spark/gpt-oss:120b,spark/qwen3.6:35b,spark/qwq:32b,spark/deepseek-r1:70b \
  --judge-model spark/gpt-oss:120b
```
Produces per-model PASS/FAIL_MODEL/FAIL_HARNESS/ERROR + quality scores → ranking → recommended default model. Record in `poc-report.md`.

## 4. Write the go/no-go
Fill `specs/124-openwiki-documenter/poc-report.md` with the 4 gates (gateway, container run, quality ≥4/5, cost/latency ≤20/10 min) and the ADOPT / DO-NOT-ADOPT decision.

## 5. Symphony wiki bootstrap (once Phase C lands)
On starting a symphony whose repo has no `openwiki/`:
- Coordinare holds non-documentation dispatch (dashboard shows the hold reason).
- It dispatches an init documenter job (`doc_mode="init"`) → seed-wiki PR → **auto-merged** once required CI is green and the bot-reviewer approves.
- Persisted marker `wiki_initialized` survives restart; on exhaustion you get a **critical notification** (`wiki_init_exhausted:<symphony>`) and the symphony holds — check the seed PR (branch protection can block auto-merge).

## 6. Flip the default (Phase D — only after ADOPT)
In `config.yaml`, `tech_writer`:
```yaml
tech_writer:
  backend: openwiki      # was: hermes
  mode: single-qwen36    # benchmark winner spark/qwen3.6:35b (reuses the existing catalog entry)
```
Served by the `openwiki-ephemeral` endpoint (`BACKEND: openwiki`, `OPENWIKI_PROVIDER: openai`, `OPENAI_BASE_URL`→gateway). The `hermes-ephemeral` endpoint is retained with `roles: [ ]` as the rollback.

**Rollback**: set `tech_writer.backend: hermes` + `mode: single-gptoss120-spark` and restore `roles: [ tech_writer ]` on `hermes-ephemeral` (exactly one endpoint may serve tech_writer — two would let a documenting job land on a container without OpenWiki's provider env). No code change.

## 7. Tests
```bash
.venv/bin/pytest tests/unit -q                          # coordinare: gate, dispatch skip, config, schema-migration, notification
.venv/bin/pytest agent/performer/tests -q               # performer: OpenWikiBackend adapter
.venv/bin/ruff check src/coordinare agent/performer/src/performer scripts/persona_bench.py
```
