# Living Wiki Documenter (spec 124)

The documenter (`tech_writer` role) maintains a **living `docs/wiki/`** — a
source-grounded record of truth other agents read — plus pointer sections in
`AGENTS.md`/`CLAUDE.md`. This is our own documenter behaving the way OpenWiki is
*meant* to be used, running on the reliable `{files}` JSON contract instead of a
flaky agentic tool loop.

> **Why not OpenWiki?** We evaluated LangChain's OpenWiki CLI as a backend
> (proof-of-concept in `specs/124-openwiki-documenter/poc-report.md` +
> `benchmark-results.md`). Its DeepAgents tool-calling was unreliable across
> every self-hosted model we tried, and cloud models are off-limits for this
> role. We dropped the backend and moved its *intent* into our own documenter.

## How it's wired
- **No new backend, no config flip.** The behavior lives in the committed
  `tech_writer` **persona** (`DEFAULT_INSTRUCTIONS` in
  `src/coordinare/services/persona_service.py`). It ships enabled by default —
  there is nothing to toggle in the operator-local `config.yaml`.
- **Backend/model**: whatever self-hosted backend already serves `tech_writer`
  (default `hermes` → `local/gpt-oss:120b` via the LiteLLM gateway). **Cloud
  models must not be used for this role.**
- **Output contract (unchanged)**: the documenter returns the standard
  `{"files":[{"path": "...", "content": "..."}]}` JSON; `main.py`'s documenting
  path commits those files and reports `status="docs_committed"`. No agentic
  filesystem loop, so it is as reliable as every other performer role.
- **Always runs** (FR-006): the documenting stage is no longer skipped when a
  PR touches no `docs/` path — the wiki tracks *code* changes, so the documenter
  runs every card and no-ops only when there is genuinely nothing to record.

## What the documenter maintains
- `docs/wiki/README.md` — the **entrypoint** that links every section page.
- `docs/wiki/<section>.md` — one canonical home per topic (architecture,
  workflows, subsystems…), grounded in real paths (no invented facts, no thin
  stubs).
- A `## Project Wiki` **pointer section** added/refreshed in `AGENTS.md` and
  `CLAUDE.md`, directing agents to `docs/wiki/README.md`.

## Deploy checklist
1. Ship the updated persona (this PR) — it is the committed default; no
   `config.yaml` edit is required for the core behavior.
2. Confirm `tech_writer` resolves to a **self-hosted** backend/model (default
   `hermes` / `local/gpt-oss:120b`). Never point it at a cloud model.
3. Ensure `LITELLM_MASTER_KEY` is exported (`set -a && source .env && set +a`)
   before launching coordinare, so the gateway is reachable.

> **⚠️ Migration from the OpenWiki POC.** If, during the earlier OpenWiki
> proof-of-concept, you set `tech_writer.backend: openwiki` (and/or added an
> `openwiki-ephemeral` endpoint) in your operator-local `config.yaml`, **revert
> it now**: set `tech_writer.backend: hermes`, `mode: single-gptoss120-local-ollama`,
> and restore `roles: [ tech_writer ]` on your `hermes-ephemeral` endpoint. The
> `openwiki` backend is removed from the image, so after rebuilding, a leftover
> `backend: openwiki` makes every documenting job fail with
> `UnsupportedBackendError`.

> **LiteLLM only.** Route the documenter through the LiteLLM gateway
> (`mode: single-gptoss120-local-ollama` → `local/gpt-oss:120b`), not the Ollama-direct
> mode (`single-gptoss120-ollama`). The `hermes-ephemeral` endpoint's mounted
> `routing.yaml` normalize-shim entry for `local/gpt-oss:120b` injects the
> LiteLLM key (`upstream_auth_env: OPENAI_API_KEY`); the Ollama-direct path
> bypasses LiteLLM.

## Symphony-init wiki gate (foundation only — trigger not wired in this release)
The wiki is built/maintained by the **normal in-card documenting stage**: on
each card the documenter refreshes `docs/wiki/` into that card's PR (`doc_mode`
defaults to `"update"`). So the wiki grows from the first card onward.

The *pre-work* init gate (hold all dispatch until a seed wiki is initialized,
`doc_mode="init"`) is **not active in this release**. `WikiInitService`
(`src/coordinare/services/wiki_init.py`) — the decision brain (detect via
`docs/wiki/README.md` on the default branch / auto-merge the seed PR on
CI-green + trusted-bot approval / attempt-budget circuit breaker / notify on
exhaustion) plus its schema-v13 persisted state — is landed and unit-tested, but
its **daemon dispatch wiring (T026) is deferred**: it needs a cardless
documenting job that opens a PR plus a per-cycle daemon dispatch/poll, validated
against a live performer. There is intentionally **no config flag** to
half-enable it — the gate and its trigger will land together in the T026
follow-up. See `specs/124-openwiki-documenter/contracts/wiki-init-gate.md`.
