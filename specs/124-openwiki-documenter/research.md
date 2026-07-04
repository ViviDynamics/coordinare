# Phase 0 Research: OpenWiki Documenter Backend & Symphony Wiki Bootstrap

> **⚠️ PIVOTED (2026-07 — spec 124).** This document describes the original
> approach: adopting LangChain's **OpenWiki** CLI as a documenter backend. That
> approach was **dropped** (OpenWiki's agentic tool-calling was unreliable across
> every self-hosted model, and cloud models are off-limits for this role).
> The shipped design instead **enhances our own `tech_writer` documenter** to
> maintain a living `docs/wiki/` via the reliable `{files}` JSON contract. See
> **`spec.md`** (the source of truth) and `docs/operators/wiki-documenter.md`.
> Sections below referring to an `openwiki` backend, `openwiki/` output paths,
> or model benchmarking are **historical**.

All items below were resolved during brainstorming, `/speckit.clarify`, and two codebase deep-map passes. **Zero `NEEDS CLARIFICATION` remain.** Each entry records the Decision, Rationale, and Alternatives considered, with codebase anchors (`file:line`) where relevant.

---

## R1. How OpenWiki is invoked and wrapped as a backend

- **Decision**: Wrap the `openwiki` Node CLI as a **one-shot subprocess backend** (`OpenWikiBackend`) mirroring `JunieBackend`/`ClaudeCodeBackend`. Run it inside the Stand (`cwd=stand.path`) with `start_new_session=True`, capture stdout/stderr in a background reader task, and set a terminal `BackendStatus` on exit. Invoke `openwiki -p <prompt>` for the initial full build and `openwiki --update --print` for incremental updates.
- **Rationale**: OpenWiki has no programmatic API (CLI-only) and DeepAgents' `LocalShellBackend` runs shell on the host — acceptable because performers already run in an ephemeral container. The one-shot pattern is the established, tested seam (`backends/base.py:26-57` Protocol; `junie.py:168-329`).
- **Alternatives**: HTTP adapter (like `opencode`) — rejected; OpenWiki has no server mode. Subclassing an existing adapter — rejected per project rule that each harness mirrors its own CLI pattern rather than inheriting.
- **Anchors**: `backends/base.py:26-57` (Protocol), `backends/__init__.py:15-57` (registry), `backends/_env_policy.py:31-69` (`build_subprocess_env`), `models.py:31-34` (`_redact_secrets`).

## R2. Pointing OpenWiki at the LiteLLM gateway (the #1 risk)

- **Decision**: Use OpenWiki's **OpenAI provider** with `OPENAI_BASE_URL=https://litellm.vividynamics.com/v1`, `OPENAI_API_KEY=${LITELLM_MASTER_KEY}`, and `OPENWIKI_MODEL_ID=<spark/...>`, set on the `openwiki-ephemeral` endpoint env. The **POC's first gate** confirms this passthrough actually reaches the gateway. If it does not, the **preferred remediation is a small patch** to OpenWiki's provider resolution (`src/constants.ts` — pass `configuration.baseURL` to the LangChain OpenAI client); only if patching is infeasible do we fall back to a permitted external provider for docs, or not adopt (FR-020).
- **Rationale**: LangChain's OpenAI client typically honors a custom base URL; this keeps documentation on the self-hosted fleet, preserving the point of benchmarking those models. Making this the first POC gate prevents wasted effort downstream.
- **Alternatives**: OpenRouter/Anthropic cloud default — rejected as primary (defeats the LiteLLM-benchmark goal); kept only as a documented fallback. Building a local OpenAI-compatible shim in front of OpenWiki — rejected as heavier than a one-line base-URL passthrough.
- **Anchors**: gateway config `config.yaml:586-603` (`conducting`); hermes-over-LiteLLM precedent `config.yaml:135-165`.

## R3. Node.js runtime and OpenWiki install in the performer image

- **Decision**: Add `openwiki` to the **global npm install in `Dockerfile.full`**. No base-image change and no new runtime needed.
- **Rationale**: **Node.js 22 LTS is already installed in `Dockerfile.base`** (NodeSource, lines 5-16). OpenWiki is a Node CLI specific to the documenter, so `:full` (used by most roles, layered by `:extra`) is the right layer; keeping it out of `:base` avoids bloating the fallback image. Python source is COPYed at build time in `Dockerfile.base` (repo-root context) — so images must be rebuilt base→full→extra when shipping (per project deploy gotcha).
- **Alternatives**: Dedicated `openwiki` image — rejected as unnecessary since Node is already present; `:base` install — rejected to keep the fallback image lean.
- **Anchors**: `Dockerfile.base:5-16` (Node 22), `Dockerfile.full` npm install block; endpoint `image:` selection read at dispatch.

## R4. In-card documenting semantics + reconciling the doc-skip gate

- **Decision**: Ongoing wiki updates run **in-card** as the existing `documenting` pipeline stage (after `qa`, before `closer`, on the card's PR pre-merge) — matching clarification Q1=B. Add `Score.doc_mode: Literal["init","update"] = "update"`. Reconcile `_should_skip_documenting` so that when the documenter backend is `openwiki`, the stage **always runs** (the wiki tracks code changes, not `docs/`-path changes); the legacy docs-path skip remains only for the hermes fallback / non-openwiki path. `doc_mode="init"` (used only by the bootstrap job) never skips.
- **Rationale**: Reuses the proven pipeline-stage hook (smaller change than a new post-merge trigger). OpenWiki's `--update` derives scope from `openwiki/.last-update.json` committed in-repo, so it works correctly against the card branch and across ephemeral containers (R7).
- **Alternatives**: Separate post-merge wiki PR (Q1 Option A) — rejected by user (couples less but is a bigger architectural change). Batched/cadence updates (Q1 Option C) — rejected for now (cost optimization, can revisit).
- **Anchors**: `main.py:81-94` (alias + `("files",)` contract), `lifecycle.py:24-27` (`CANONICAL_ORDER`), `dispatch_performer.py:313-321` (`_should_skip_documenting`), `dispatch_performer.py:771-784` (application), `dispatch_performer.py:1204-1221` (backend/model resolution).

## R5. Wiki-initialization prerequisite gate (mirror env-bootstrap)

- **Decision**: New `WikiInitService` mirroring `EnvCacheService`. At symphony start (daemon `start()` ~line 2480, after board reconcile, before the poll loop; seeded in `__main__.py` like `EnvCacheService.initialise` at ~1009-1017), detect whether the repo has a wiki (`openwiki/` on the default branch + a persisted `wiki_initialized` marker). If absent, dispatch a one-shot documenter job in `doc_mode="init"`; **hold all non-documenting/non-wiki-init dispatch** via a dispatch-time gate that mirrors the env-cache hold (`dispatch_performer.py:1291-1337`). Persist a restart-safe circuit-breaker; on exhaustion, notify + hold (never silent deadlock).
- **Rationale**: The env-bootstrap gate is a proven, restart-safe, circuit-broken prerequisite gate; the wiki gate is its faithful mirror. Dispatch-time hold (not a startup block) means restart-safety and per-symphony isolation come for free.
- **Alternatives**: A brand-new graph node/edge — rejected as more invasive than reusing the dispatch-hold seam. Blocking the daemon at startup until the wiki exists — rejected (breaks multi-symphony + restart semantics).
- **Anchors**: `daemon.py:2480-2526` (hook), `__main__.py:1009-1017` (service seed), `dispatch_performer.py:1291-1337` (hold pattern), `models/env_cache.py:12-72` (state fields), `daemon.py:274-305` (`_persist_env_cache`).

## R6. Persisted wiki-init state + schema migration

- **Decision**: Co-locate persisted wiki-init fields on `EnvCacheStateSnapshot` (per-symphony, already persisted): `wiki_attempts:int=0`, `wiki_exhausted:bool=False`, `last_wiki_init_at:datetime|None`, `last_wiki_init_succeeded:bool|None`, `last_wiki_init_error:str|None`, plus `wiki_initialized:bool=False` (the marker). Add transient live fields to `EnvCacheState` (`wiki_in_flight:bool=False`, reset on restart). Bump `CURRENT_SCHEMA_VERSION` 12 → 13. Old snapshots load with defaults (Pydantic) — no explicit migration code.
- **Rationale**: Mirrors the env-bootstrap persisted/transient split (`daemon.py:_persist_env_cache` deliberately drops transient fields). Co-location avoids a second snapshot model while keeping wiki logic in its own service. Additive defaults = backward-compatible, matching the v1→v12 evolution pattern.
- **Alternatives**: A separate top-level `wiki_init` snapshot section — rejected (more surface, no benefit). A new standalone snapshot model — rejected (duplicate plumbing).
- **Anchors**: `state_store.py:18` (`CURRENT_SCHEMA_VERSION=12`), `state_store.py:286-324` (`EnvCacheStateSnapshot`), `models/env_cache.py:12-72`.

## R7. Incremental scope must survive ephemeral containers

- **Decision**: Rely on OpenWiki's `openwiki/.last-update.json` (committed in-repo) as the source of "commits since last documented," **not** the container-local `~/.openwiki/openwiki.sqlite` checkpoint (ephemeral).
- **Rationale**: Each documenter run is a fresh container; local SQLite is discarded. The in-repo marker is the durable, correct source and is exactly what `--update` consults.
- **Alternatives**: Mounting a persistent SQLite volume — rejected (adds host state, breaks single-shot isolation).
- **Anchors**: OpenWiki research report (SQLite at `~/.openwiki/openwiki.sqlite`; `openwiki/.last-update.json` metadata).

## R8. Bootstrap-PR auto-merge — a scoped exception to human-only merges

- **Decision**: For the **init-bootstrap PR only**, `WikiInitService` performs auto-merge: poll `github.check_mergeability(pr_node_id)` for required-CI-green (`mergeable_raw == "MERGEABLE"` / `merge_state_status` clean of `DIRTY`/`BEHIND`) and confirm a **trusted-bot approval** via `github.get_pr_reviews` + `classify_reviewer`, then call `github.squash_merge(pr_node_id)`. This **intentionally bypasses** the normal human-approval merge gate. Ongoing wiki updates (FR-018) keep the normal human gate. If branch protection blocks the auto-merge (`squash_merge` returns `merged=False` or raises `PermanentGitHubError`), fall to hold + notify (the "auto-merge blocked" edge case).
- **Rationale**: Coordinare's invariant is "only humans approve merges" (`monitor_pr.py:221`; `check_mergeability.mergeable` requires `reviewDecision == APPROVED`). The user explicitly chose to auto-merge the bootstrap so a new symphony isn't blocked on a person. Scoping the exception to the single seed PR, gating it on CI + bot review, and degrading safely on branch protection keeps the blast radius minimal. Flagged in the plan's Complexity Tracking.
- **Alternatives**: Hold-for-human (safe but stalls every new symphony) and direct-commit-to-main (skips CI/review) — both rejected in brainstorming §C.
- **Anchors**: `services/github.py` `check_mergeability`/`squash_merge`/`get_pr_reviews` (GraphQL `CHECK_MERGEABILITY_QUERY`, `SQUASH_MERGE_MUTATION`), `monitor_pr.py:188-226` (reviewer classification), `merge_pr.py:17-145` (normal merge flow), `config.yaml:276-279` (`human_reviewers`/`trusted_bot_reviewers`).

## R9. Config wiring for a new backend + gated default switch

- **Decision**: Add an `openwiki-ephemeral` performer_endpoint (`roles: [tech_writer]`, `image: coordinare-performer:full`, env `BACKEND=openwiki`, `OPENAI_BASE_URL`/`OPENAI_API_KEY=${LITELLM_MASTER_KEY}`); add a `model_endpoint` (e.g. `openwiki-gptoss120`) and a `single-...` `mode` for the benchmark-selected model. Leave `tech_writer.backend: hermes` (+ its mode) in place; the **gated switch** (Phase D) changes `tech_writer.backend` to `openwiki` and its `mode` to the openwiki mode, keeping the hermes block for one-line rollback. The 080 hard-cut forbids inline `model`/`base_url` on roles, so model selection flows through the catalog; the `OpenWikiBackend` maps the resolved `model` → `OPENWIKI_MODEL_ID` and reads `OPENAI_BASE_URL`/`OPENAI_API_KEY` from the endpoint env.
- **Rationale**: This is exactly how hermes/codex/pi are wired (endpoint + catalog + role). Rollback = revert `tech_writer.backend`/`mode` (SC-005).
- **Alternatives**: Inline model on the role — rejected (config.py `_reject_removed_inline_model_fields` fails loudly, 080).
- **Anchors**: `config.yaml:135-165` (hermes endpoint), `config.yaml:495-505` (tech_writer role), `config.yaml:296-337` (catalogs), `config.py:349-462` (Endpoint/ModelEndpoint/Mode), `config.py:470-535` (PerformerRoleConfig), `config.py:1050-1082` (`resolve_performer_dispatch_model`).

## R10. Cross-model benchmark + POC

- **Decision**: Extend `scripts/persona_bench.py` with a `documenter` `RoleTask` (role `documenting`) + `grade_documenter()` deterministic grader (status `docs_committed` + wiki content present via `docker exec`) + a rubric, and run it across the 4 tool-calling models (`spark/gpt-oss:120b`, `spark/qwen3.6:35b`, `spark/qwq:32b`, `spark/deepseek-r1:70b`) via the performer `/jobs` API. Two-stage grading (deterministic + LLM judge 1-5) yields verdicts (PASS/FAIL_MODEL/FAIL_HARNESS/ERROR) → a ranking + recommended default model. The **POC** additionally runs OpenWiki end-to-end on the real "website" symphony repo to validate gateway passthrough, container execution, quality (judge + human spot-check), and cost/latency, producing the written go/no-go report.
- **Rationale**: `persona_bench` already models role×backend×model×fixture with exactly this grading and dispatch; extending it is the least-effort, convention-matching path. The website-repo POC provides the realistic quality/cost signal the grid cannot.
- **Alternatives**: A bespoke benchmark harness — rejected (persona_bench already exists and is the project standard).
- **Anchors**: `scripts/persona_bench.py` (`RoleTask` ~307-318, `TASKS` ~321-431, `run_cell` ~628-808, graders ~188-301, judge ~437-477, verdicts ~105-156).

## R11. Documenter result contract (no downstream change)

- **Decision**: `OpenWikiBackend` emits the existing documenter contract: JSON with a `files` key → `PerformerResponse.files_modified`, terminal status `docs_committed` on success or `error` with `reason`. (Documenter has no `blocked` status in code; the spec's generic "blocked" maps to `error`/hold at the orchestration layer.)
- **Rationale**: SC-009 — downstream orchestration must be unchanged. The performer wrapper (`main.py`) already enforces `("files",)` for the `documenting` stage.
- **Anchors**: `main.py:87-94` (required keys), `protocol.py:101-120` (`PerformerResponse`), `main.py:2863-2917` (terminal handling).
