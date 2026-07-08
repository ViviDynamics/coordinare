# coordinare Development Guidelines

## Stack

Python 3.12+, FastAPI, LangGraph, pydantic v2, pydantic-settings, structlog, httpx, gql[aiohttp], prometheus-client, asyncio (stdlib throughout).

## Commands

The top-level `Makefile` is the canonical entry point — run `make` (or `make help`) to see
every task. It is a thin façade over `bin/` and `.venv` (nothing is reimplemented).

```sh
make help          # list all targets, grouped (dev / test / build / run / release / clean)
make test          # unit tests (env-unset applied automatically)
make test-all      # the WHOLE tests/ tree (unit + contract) — run before pushing
make lint          # ruff check src/ tests/   (make fmt = auto-fix)
make ci            # full CI parity — exactly what `bin/build --all` runs
make run           # start the daemon (sources .env first, or fails loudly)
```

Raw fallbacks (equivalent to the make targets above):

```sh
env -u COORDINARE_INFERENCE_MAX_TOKENS -u HERMES_CONTEXT_WINDOW .venv/bin/pytest tests/unit/
.venv/bin/ruff check src/ tests/          # lint
.venv/bin/ruff check --fix src/ tests/    # auto-fix lint
```

Always `set -a && source .env && set +a` before launching the coordinare daemon directly —
`config.yaml` expands `${VAR}` placeholders at load time and silently uses empty strings
without the env. (`make run` / `make start` do this for you.)

## Architecture

**`CoordinareState`** is a plain `dict[str, Any]` threaded through everything. Keys of note:
- `env_cache`: `dict[str, EnvCacheState]` — per-symphony bootstrap tracking
- `performer_services`: `dict[str, <service>]` — keyed by performer ID
- `symphony_github_services`: `dict[str, GitHubService]` — keyed by symphony name
- `performer_endpoints`: `dict[str, PerformerEndpointState]` — HTTP performer pool

**`Daemon`** drives the main poll loop in `daemon.py`. Each cycle iterates symphony configs and dispatches work via LangGraph nodes.

**Dispatch nodes** live in `src/coordinare/graph/nodes/`. They receive state, call performer services, and return state updates.

**Config** loads from `config.yaml` (with `${ENV_VAR}` expansion) via `CoordinareConfiguration` in `src/coordinare/config.py`. `SymphonyConfig.effective_config(global_config)` merges symphony-level overrides onto global defaults.

## Code Conventions

- `from __future__ import annotations` at the top of every module.
- Pydantic models that use `Path` or `datetime` as field types must import them at **runtime** (not under `TYPE_CHECKING`) — pydantic v2 calls `get_type_hints()` at class creation. Suppress the TC003 lint warning with `# noqa: TC003`.
- `TYPE_CHECKING` blocks are fine for everything else (protocols, heavy imports, circular refs).
- Background `asyncio.Task` references must be stored (e.g. in a `set` on the owning object with `add_done_callback(set.discard)`) to prevent GC — ruff RUF006 enforces this.
- Use `inspect.signature()` to check for optional parameters before passing them when calling across protocol boundaries (avoids breaking non-HTTP service implementations).
- Structured logging via `structlog.get_logger(__name__)`. Log keys use `snake_case.dot.separated` event names.

## Testing

- Unit tests in `tests/unit/test_<NNN>_<feature>.py`.
- Async tests use `@pytest.mark.asyncio`.
- Tests that call config discovery must mock `Path.cwd()` — a real `config.yaml` exists in the project root and will be picked up otherwise.
- `VolumeMount.container_path` is a `PurePosixPath`; use `str()` when comparing to string literals in tests.

## Workflow

- Feature branches named `<NNN>-<feature-name>` matching the spec directory.
- Specs live in `specs/<NNN>-<name>/` (spec.md, plan.md, tasks.md, data-model.md, contracts/).
- Run `/speckit.analyze` before `/speckit.implement`.
- PR target is `main`; squash merge.

<!-- MANUAL ADDITIONS START -->

## Model selection (080 — dual-model orchestration)

Model selection is unified behind three root-level config catalogs, referenced by name:

- **`endpoints`** — where models are served. `kind` splits **native** vendor clouds (`openai`/`anthropic`: harness uses its own client, no `base_url`, no proxy) from **self-hosted** (`litellm`/`ollama`/`vllm`: requires `base_url`, proxy-eligible).
- **`model_endpoints`** — named `{model @ endpoint}` pairs (the unit of model selection).
- **`modes`** — named orchestration behaviors: `strategy` ∈ `single | always | conditional | think_once`, plus `tool`/`thinking`/`classifier` model_endpoint refs and params.

Each performer is `{ backend: <harness>, mode: <mode-name> }`. **Inline `performers.<role>.model` is removed (hard cut)** — its presence is a load-time error. Resolution chain: `performer.mode → modes → model_endpoints → endpoints`, validated at load (`ProjectConfiguration._validate_orchestration_catalogs`). Coordinare resolves the dispatch model via `resolve_performer_dispatch_model(role)` and, for non-`single` modes, the full block via `resolve_performer_orchestration(role)` (rides `card_context['orchestration']` → `metadata`).

**Dual-model proxy** (`agent/performer/src/performer/proxy/`): for non-`single` modes the performer launches an in-container aiohttp reverse proxy (`DualModelProxy`, generalizing the 073 `ClaudeCodeShim` seam) and points the backend's provider base-URL env at it (`proxy/launch.py`). The proxy normalizes the CLI request to a canonical `LLMRequest`, runs the strategy (planner `think` with tools hidden → executor `act` with the plan injected as a system message), and assembles one wire-correct response. **Constraint: `hermes` has no provider-base-URL override → `strategy: single` only.** SSE streaming + the live Spark validation round are the remaining 080 follow-ups.

## Recent Changes

- **075-implementer-ci-gate**: `_evaluate_ci_gate` in `src/coordinare/graph/nodes/monitor_performer.py` runs at the implementer→reviewer hand-off, emitting `pass | hold | bounce | escalate` per the spec. Required-checks resolver in `src/coordinare/services/required_checks_resolver.py` falls back through `persona_check_map` → `branch_protection` → `all_head_checks`. PR rollup comments in `notify.py` are deduped per `(head_sha, verdict, required, failed-names)` signature.

- **076-qa-cycle (dispatcher dedup + lifecycle correctness)**: 5 user stories closing the 2026-05-28 duplicate-dispatch incident.
  - **In-flight guard + per-card mutex** (`src/coordinare/services/dispatch_guard.py`): `dispatch_performer` now wraps its body in `async with dispatch_mutex(card_id, performer_stage)` + `await check_inflight(state, card_id, performer_stage)`. Refuses dispatch when an in-flight session is still alive; emits `dispatch_performer.in_flight_guard_tripped`.
  - **Startup reconciliation pass** (`src/coordinare/services/reconciliation.py`): `run_startup_reconciliation()` is called from `daemon.py` boot. Enumerates `coordinare.spec_version=076`-labelled Docker containers via `DockerExecutor` and emits one of `adopted | reaped_and_replaced | fresh_dispatched | skipped_persistent | orphan_swept` per in-flight card.
  - **Docker labels** (`src/coordinare/services/performer_lifecycle.py` + `http_performer_service.py`): every ephemeral container now carries `coordinare.session_id`, `coordinare.card_id`, `coordinare.performer_stage`, `coordinare.daemon_started_at`, `coordinare.spec_version="076"` plus the existing `coordinare.performer.id`. `_active_jobs` is keyed on the coordinare-allocated `session_id` (not the job-runner's `job_id`).
  - **Wedge invariant** (`detect_wedged_state`): runs at end-of-cycle in `daemon.py`. When `active_card` is pinned but no session exists and `phase=idle`, releases the pin by default (clarification Q1). After ≥3 wedges in 24h, promotes to BLOCKED.
  - **Board ↔ state reconciliation** (`reconcile_board_state`): per-cycle check. Releases the pin when the board moved the card back to TODO/BACKLOG while local pins IN_PROGRESS/IN_REVIEW/BLOCKED. Forward divergences (board=IN_REVIEW, local=IN_PROGRESS) are deferred to the existing startup `_reconcile_with_board` path.
  - **PR-artefact write-through** (`monitor_performer._record_pr_artefacts`): every successful turn's `pr_url`/`pr_node_id`/`pr_number`/`head_sha`/`pushed_branch` is mirrored to BOTH `state.active_card` and `state.active_sessions[card_id]` with a `pr_artefacts_recorded_at` audit timestamp. Prevents the "successful turn forgotten" failure mode.
  - **Idle-timeout retry counter** (`src/coordinare/services/retry_counter.py`): per-`(card_id, performer_stage)` rolling counter, default 2 retries per 24h window, then BLOCKED. Persists across daemon restarts via `idle_timeout_retries` on `PersistedSession`. Caps the qwen-stall failure mode.
  - **Drain-or-reap relay handoff** (`drain_or_reap`): on `partial_progress` + idle-timeout retry, the prior container is drained (5 s budget) or force-stopped (5 s budget) before fresh dispatch — closes FR-007 "no agent_dispatch={} while prior container still alive".
  - **Multi-PR divergence detection** (`detect_multi_pr_divergence` + `github.list_prs_by_branch_prefix`): immediately before each dispatch, queries GitHub for open PRs matching `coordinare/<card_node_id>/`. >1 match → refuse dispatch.
  - **Notification dedup** (`notify._should_emit_card_dispatched`): suppresses `card_dispatched` events when the most recent reconciliation pass produced `ADOPTED` or `SKIPPED_PERSISTENT`.
  - **Schema v6 → v7** (`state_store.py`): 5 new PersistedSession fields — `idle_timeout_retries`, `pr_artefacts_recorded_at`, `multi_pr_divergence`, `wedge_count_window`, `reconciliation_decisions_last_startup`. v1–v6 snapshots load with safe empty defaults.
  - **Structured-log vocabulary** (grep recipes for operators):
    - Reconciliation: `daemon.reconciliation_pass_(started|complete|aborted_docker_unreachable|budget_exceeded)`, `daemon.session_adopted`, `daemon.orphan_swept`, `daemon.reap_failed`, `check_board.stale_session_reconciled`
    - In-flight guard: `dispatch_performer.(in_flight_guard_tripped|in_flight_guard_probe_failed|mutex_waited|multi_pr_divergence_refused)`
    - Wedge invariant: `daemon.wedged_state_detected`, `daemon.wedge_resolution`
    - Board sync: `daemon.board_state_reconciled`, `daemon.board_state_diverged_deferred`
    - PR write-through: `monitor_performer.pr_artefacts_recorded`
    - Retry counter: `monitor_performer.idle_timeout`, `monitor_performer.idle_timeout_exhausted`
    - Notification dedup: `notify.card_dispatched_suppressed`
  - **Config**: new top-level `dispatcher_dedup:` block with 8 tunables (defaults reflect clarifications Q1–Q5).

- **090-baseline-repair-autonomy**: three independently opt-in layers, all **default-off** so routing, QA verdicts, and merge decisions stay byte-identical to a pre-spec-090 build (**SC-006**). All three live under the symphony-level `persona_scope:` block in `config.py` (`PersonaScopeConfig`):
  - **L1 `baseline_prevention_gate`** (`{ enabled: false }`) — when enabled, the closer refuses to merge while the **base** branch's *required* checks are red (`evaluate_base_gate` in `src/coordinare/services/base_gate.py`, consumed by `monitor_pr.py`). Re-read every cycle, never latched; fail-safe — a `None`/unreadable base rollup yields INDETERMINATE/PROCEED and never hard-blocks (FR-005).
  - **L2 `baseline_classification_gate`** (`{ enabled: false }`) — when enabled, **observe-only**: classifies each head failure INHERITED / INTRODUCED / FLAKE / UNKNOWN by comparing a reason-sensitive failure signature (`failure_signature.make_failure_signature`) against a merge-base baseline index (`monitor_performer._build_baseline_index` → `failure_classification.classify_failure_origin`). Emits the classification only; takes no autonomous action and never affects the verdict.
  - **L3 `inherited_repair_gate`** (`{ enabled: false, max_repair_attempts_per_head: 1 }`) — when enabled, the implementer may repair INHERITED *stable* failures on the card's existing branch into its open PR, gated by a **dual test-integrity guard** (static `test_integrity_guard.analyze_diff` on the hot path **plus** an automated adversarial `diagnostic`-role reviewer in fresh context, `monitor_performer._dispatch_repair_reviewer`). Either veto — or any uncertainty — rejects and escalates (`phase="blocked"`); the loop never auto-merges and fails safe. `max_repair_attempts_per_head` is a per-head budget (`0` = repair disabled even when `enabled`; range 0–20), tracked via schema-v9 `inheritance_repair_counter` + append-only `repair_audit`.
  - **L3 branch-protection prerequisite**: enabling L3 requires GitHub branch protection's **"Dismiss stale pull request approvals when new commits are pushed"** to be ON for the protected branch, so a repair push re-opens review rather than riding a prior human approval into an auto-merge.
  - **FR-011**: the raw PR diff text is never logged — only a length summary on success / an error type on failure.

<!-- MANUAL ADDITIONS END -->
