# OpenWiki Documenter — Cross-Model Benchmark Results

> **📎 HISTORICAL EVIDENCE (spec 124).** This is the proof-of-concept/benchmark
> record that led us to **drop the OpenWiki backend** and instead enhance our own
> `tech_writer` documenter (see `spec.md`). Retained as the justification for that
> decision — the `openwiki` backend it evaluates no longer exists in the codebase.

**Date**: 2026-07-04 · **Method**: `openwiki --init --print` on an identical tiny git repo per model, via the LiteLLM gateway (`OPENWIKI_PROVIDER=openai`, `OPENAI_BASE_URL`→gateway). `pages`/`marker` = authoritative "produced a usable wiki" signal. Full analysis in [poc-report.md](./poc-report.md).

| Model | exit | time | wiki pages | marker | verdict |
|---|---|---|---|---|---|
| **`spark/qwen3.6:35b`** | 0 | 53s | 1 | ✅ | **PASS** (recommended default) |
| `spark/gpt-oss:120b` | 1 | 94s | 0 | ❌ | FAIL_MODEL — retry error, no wiki |
| `spark/qwq:32b` | 124 | 480s | 0 | ❌ | FAIL — timed out |
| `spark/deepseek-r1:70b` | 0 | 298s | 0 | ❌ | FAIL_MODEL — plan narrative, no files |

**Recommended default model: `spark/qwen3.6:35b`** (only reliable pass; fastest). The current `gpt-oss:120b` documenter model does not drive OpenWiki's agentic loop.

> Note: this is the direct-CLI benchmark run during the POC. The `scripts/persona_bench.py` `documenter` fixture (T013/T014) formalizes the same grading through the performer `/jobs` API for repeatable CI-style runs against the `openwiki-ephemeral` endpoint once the image ships.
