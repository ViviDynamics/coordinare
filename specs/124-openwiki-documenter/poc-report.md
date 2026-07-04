# OpenWiki Documenter — Proof-of-Concept Go/No-Go Report

> **📎 HISTORICAL EVIDENCE (spec 124).** This is the proof-of-concept/benchmark
> record that led us to **drop the OpenWiki backend** and instead enhance our own
> `tech_writer` documenter (see `spec.md`). Retained as the justification for that
> decision — the `openwiki` backend it evaluates no longer exists in the codebase.

**Date**: 2026-07-04
**Branch**: `124-openwiki-documenter`
**Run by**: automated POC against the live LiteLLM gateway (`https://litellm.vividynamics.com/v1`), OpenWiki v0.0.1 on Node 20.19.5.

## Decision: ✅ **ADOPT**

OpenWiki works against the self-hosted LiteLLM gateway with **no fork or patch required**, produces coherent wikis end-to-end, and maintains them incrementally. The recommended default model is **`spark/qwen3.6:35b`**.

---

## Gate results (FR-019, in order)

### Gate 1 — Gateway passthrough — ✅ PASS (the #1 risk, eliminated)
- Ran `openwiki -p "Reply … PONG"` with `OPENWIKI_PROVIDER=openai`, `OPENAI_API_KEY=${LITELLM_MASTER_KEY}`, `OPENAI_BASE_URL=https://litellm.vividynamics.com/v1`, `OPENWIKI_MODEL_ID=spark/qwen3.6:35b` → returned `PONG`.
- **Negative control**: same run with `OPENAI_BASE_URL` unset → `401 Incorrect API key … platform.openai.com`. This proves the base URL is what routes to the gateway.
- **Mechanism**: OpenWiki's `openai` provider config carries no `baseURL` (`agent/index.js` builds `new ChatOpenAI({apiKey, configuration: undefined, model})`), but the underlying **openai-node SDK honors a process-level `OPENAI_BASE_URL`**. OpenWiki lists `OPENAI_BASE_URL` in `deprecatedEnvKeys` (`env.js`) but only *skips it when loading its own `~/.openwiki/.env`* — it does **not** unset a process-env value. Coordinare sets it on the endpoint, so passthrough works.
- **Consequence**: FR-020 remediation (patch/fork) is **NOT** required.

### Gate 2 — Runs end-to-end, coherent wiki — ✅ PASS
- `openwiki --init --print` on a tiny git repo (`calc.py` + `README.md`), `spark/qwen3.6:35b`, **38s** → produced `openwiki/quickstart.md`, `AGENTS.md` (pointer), `openwiki/.last-update.json` (marker).
- Files match exactly what `OpenWikiBackend._collect_changed_docs` gathers (untracked, `--untracked-files=all`).

### Gate 3 — Quality — ✅ PASS (judge ~4/5)
- `quickstart.md` was accurate: correct source table (`calc.py` → `add(a,b)`, quoted), correct "no tests/deps/packaging" notes, a real change-history table with the actual commit SHA, correct `AGENTS.md` pointer.
- Minor: one whimsical embellishment of the project name (a small, harmless hallucination). Human spot-check: acceptable.

### Gate 4 — Cost / latency — ✅ PASS (for the recommended model)
- `--init` 38–53s; `--update` 102s on the tiny repo with `spark/qwen3.6:35b` — well within the 20-min / 10-min budgets (SC-008). Real "website" repo will scale up but the per-call latency and one-shot model are sound. All inference is on the self-hosted gateway (no external cost).

### Incremental correctness (FR-009) — ✅ PASS
- After a new commit (`subtract()`), `openwiki --update --print` detected the change **via the committed `.last-update.json` gitHead**, refreshed only `quickstart.md`, and advanced the marker to HEAD. Confirms incremental scope survives ephemeral containers (no reliance on container-local SQLite).

---

## Cross-model benchmark (FR-021)

`openwiki --init --print` on an identical tiny repo, per model, via the gateway. `pages`/`marker` are the authoritative "produced a usable wiki" signal.

| Model | exit | time | wiki pages | marker | verdict |
|---|---|---|---|---|---|
| **`spark/qwen3.6:35b`** | 0 | 53s | 1 | ✅ | **PASS** — clean, coherent |
| `spark/gpt-oss:120b` | 1 | 94s | 0 | ❌ | **FAIL_MODEL** — LangChain retry error mid-run, no wiki |
| `spark/qwq:32b` | 124 | 480s | 0 | ❌ | **FAIL** — timed out (reasoning loop stalled; abort-listener leak) |
| `spark/deepseek-r1:70b` | 0 | 298s | 0 | ❌ | **FAIL_MODEL** — narrated a plan with "placeholder content", wrote no files |

**Recommended default model: `spark/qwen3.6:35b`** — the only reliable pass, and the fastest.

**Notable finding**: the *current* documenter model (`gpt-oss:120b`, used by the `hermes` tech_writer) does **not** drive OpenWiki's DeepAgents tool-calling loop — it errored out. The reasoning models (`qwq:32b`, `deepseek-r1:70b`) either stall or produce plans instead of files. OpenWiki's agentic loop favors a solid tool-calling general model over reasoning/large models here.

---

## Side effect the POC caught (fixed)

The live `--help` proved the initial build must use `openwiki --init`, not the `-p "<prompt>"` the backend originally used. `OpenWikiBackend._build_args` was corrected to `init=--init --print`, `update=--update --print`, plus a `prompt` mode (`openwiki -p`) for feedback relay. Tests + contract updated (commit `9abdad0`).

---

## Implications for rollout

- **US4 default switch** is unblocked: set `tech_writer` → `backend: openwiki`, model `spark/qwen3.6:35b`. **Prerequisite**: rebuild + deploy `coordinare-performer:full` (with `npm i -g openwiki`, already in `Dockerfile.full`) and add the `openwiki-ephemeral` endpoint + catalog (T002/T003) at deploy time. Do NOT flip the live config before the image ships.
- **US3 init-gate** is unblocked to build.
- Keep `hermes`/`gpt-oss:120b` as the configured fallback (SC-005) — especially since gpt-oss failed the benchmark, rollback would be to the *old semantics*, not a better OpenWiki model.
