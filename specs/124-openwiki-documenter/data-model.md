# Phase 1 Data Model: OpenWiki Documenter Backend & Symphony Wiki Bootstrap

> **⚠️ PIVOTED (2026-07 — spec 124).** This document describes the original
> approach: adopting LangChain's **OpenWiki** CLI as a documenter backend. That
> approach was **dropped** (OpenWiki's agentic tool-calling was unreliable across
> every self-hosted model, and cloud models are off-limits for this role).
> The shipped design instead **enhances our own `tech_writer` documenter** to
> maintain a living `docs/wiki/` via the reliable `{files}` JSON contract. See
> **`spec.md`** (the source of truth) and `docs/operators/wiki-documenter.md`.
> Sections below referring to an `openwiki` backend, `openwiki/` output paths,
> or model benchmarking are **historical**.

Entities, fields, validation, and state transitions. Anchors are `file:line` in the current codebase. **New** = added by this feature; **Edit** = modified; **Reuse** = referenced unchanged.

---

## 1. Project Wiki (repository artifact — not a code model)

The living record of truth, produced by OpenWiki into the target repo:

| Element | Description | Validation / Notes |
|---|---|---|
| `openwiki/` directory | Structured markdown wiki pages | Presence on the default branch = "wiki exists" for the init gate |
| `openwiki/.last-update.json` | Metadata: last-documented commit SHA | Durable source of incremental scope (R7); committed in-repo |
| `AGENTS.md` / `CLAUDE.md` pointers | Concise "read `openwiki/`" references appended by OpenWiki | Must not corrupt coordinare-owned sections (edge case) |

**Detection rule (init gate)**: a symphony's repo "has a wiki" iff `openwiki/` exists on the default branch **or** the persisted `wiki_initialized` marker is true.

---

## 2. `Score.doc_mode` — **Edit** (`agent/performer/src/performer/models.py`, after ~line 143)

```python
doc_mode: Literal["init", "update"] = "update"
```

- **Purpose**: tells `OpenWikiBackend` whether to full-build (`init`) or incremental-update (`update`).
- **Default** `"update"` → backward compatible; existing dispatch paths need no change.
- **Set to `"init"`** only by the wiki-init bootstrap dispatch (`WikiInitService`).
- **Validation**: `Literal` restricts to the two values; `Score.model_config = {"extra": "ignore"}` (line 145) tolerates older payloads.
- **Consumers**: `OpenWikiBackend.start()` (arg construction); `dispatch_performer` doc-skip reconciliation (never skip when `doc_mode == "init"`).

## 3. `OpenWikiBackend` — **New** (`agent/performer/src/performer/backends/openwiki.py`)

Implements the `BackendAdapter` Protocol (`backends/base.py:26-57`). Instance state mirrors `JunieBackend`:

| Field | Type | Notes |
|---|---|---|
| `_executable` | `str` | `os.environ.get("OPENWIKI_EXECUTABLE", "openwiki")` |
| `_proc` | `asyncio.subprocess.Process \| None` | the CLI process |
| `_status` | `BackendStatus` | terminal state reported to the poller |
| `_reader_task` | `asyncio.Task \| None` | background `_wait_and_parse` |
| `_event_buffer` | `deque[BackendEvent]` | `maxlen=200` |
| `_stand` / `_score` | `Stand \| None` / `Score \| None` | dispatch inputs |

**Behavioral contract** (details in `contracts/openwiki-backend.md`):
- `start()` builds the prompt + args, resolves env via `build_subprocess_env(...)`, maps `model` → `OPENWIKI_MODEL_ID`, reads `OPENAI_BASE_URL`/`OPENAI_API_KEY` from endpoint env, launches with `cwd=stand.path`, `start_new_session=True`.
- `get_status()` / `drain_events()` non-blocking (Protocol).
- `_wait_and_parse()` sets `BackendStatus(state="done")` on exit 0, else `state="error"` with `error_reason` (stderr, redacted via `BackendEvent`).
- `stop()` kills the process group (SIGKILL + psutil fallback), 5 s wait.

## 4. `EnvCacheState` (live) — **Edit** (`src/coordinare/models/env_cache.py:12-72`)

Add transient live fields (NOT persisted; reset on restart, like `bootstrap_in_flight`):

```python
wiki_in_flight: bool = False          # a wiki-init job is mid-run
```

Persisted values are read back from the snapshot (below) into this live model at restore.

## 5. `EnvCacheStateSnapshot` (persisted) — **Edit** (`src/coordinare/state_store.py:286-324`)

Add wiki-init durable fields (all with safe defaults → backward compatible):

```python
wiki_initialized: bool = False              # the marker: seed wiki merged to default branch
wiki_attempts: int = 0                       # failed-init counter (circuit breaker)
wiki_exhausted: bool = False                 # attempts exhausted → hold + notify
last_wiki_init_at: datetime | None = None
last_wiki_init_succeeded: bool | None = None
last_wiki_init_error: str | None = None
```

**Persist mapping**: extend `_persist_env_cache` (`daemon.py:274-305`) to snapshot these (transient `wiki_in_flight` is deliberately dropped).

**Schema version**: `CURRENT_SCHEMA_VERSION: int = 13` (was 12, `state_store.py:18`). Old v12 snapshots load with the defaults above — no migration code needed (Pydantic defaults).

**Attempt budget (config, not snapshot)**: a configurable `wiki_init_max_attempts` (default **3**, mirroring `env_bootstrap_max_attempts`) governs when `wiki_attempts` trips `wiki_exhausted`. Lives alongside the env-bootstrap max-attempts setting in the coordinare/symphony config.

### State transitions (wiki-init circuit breaker)

```
                     ┌─────────────────────────────────────────────┐
   symphony start →  │ wiki_initialized? ──yes──▶ proceed (no gate) │
                     └───────────────┬─────────────────────────────┘
                                     │ no
                                     ▼
                 wiki_exhausted? ──yes──▶ HOLD dispatch + notify(critical, dedup)
                                     │ no
                                     ▼
             dispatch documenter job (doc_mode="init"), set wiki_in_flight=True
                                     │
              ┌──────────────────────┴───────────────────────┐
          success                                          failure
              │                                               │
   open bootstrap PR → auto-merge (CI green + bot approval)   wiki_attempts += 1
              │                                       last_wiki_init_error set
      merged? ─yes─▶ wiki_initialized=True                    │
              │       last_wiki_init_succeeded=True   wiki_attempts >= budget?
        no (branch-protected / conflict)                       │
              └──▶ HOLD + notify (auto-merge blocked)     yes ▶ wiki_exhausted=True → HOLD + notify
```

`wiki_in_flight` is cleared on every terminal outcome. Dispatch-time gate blocks all non-`documenting`/non-wiki-init stages while `not wiki_initialized`.

## 6. `EventType.wiki_init_exhausted` — **New** (`src/coordinare/models/notification.py`)

New enum value alongside `env_bootstrap_exhausted`. Dispatched via `NotificationService.dispatch(NotificationEvent(event_type=EventType.wiki_init_exhausted, severity=critical, source="wiki_init", dedup_key=f"wiki_init_exhausted:{symphony}", payload={...}))` on exhaustion or auto-merge-blocked hold.

## 7. Config additions — **Edit** (`config.yaml`) / **Reuse** (`config.py` models)

- **New endpoint** `openwiki-ephemeral`: `roles: [tech_writer]`, `image: coordinare-performer:full`, env `BACKEND=openwiki`, `OPENAI_BASE_URL=https://litellm.vividynamics.com/v1`, `OPENAI_API_KEY=${LITELLM_MASTER_KEY}`, `OPENWIKI_MODEL_ID` (resolved model).
- **New `model_endpoint`** (e.g. `openwiki-gptoss120`) + **new `mode`** (e.g. `single-openwiki-gptoss120`) selecting the benchmark-chosen model.
- **`tech_writer` role**: gated switch of `backend: hermes → openwiki` and `mode` to the openwiki mode (Phase D); hermes block retained for rollback.
- **Reuse** `PerformerRoleConfig` / `Endpoint` / `ModelEndpoint` / `Mode` (`config.py:349-535`) — no model changes expected; the 080 hard-cut keeps model selection in the catalog.

## 8. `PerformerResponse` — **Reuse** (`agent/performer/src/performer/protocol.py:101-120`)

Unchanged. Documenter emits `status="docs_committed"`, `files_modified: list[str]`, or `status="error"` + `reason`. Enforced-key `("files",)` for the `documenting` stage (`main.py:87-94`). SC-009: no downstream change.

## 9. Benchmark `RoleTask` (documenter) — **New** (`scripts/persona_bench.py`)

A `RoleTask(label="documenter", role="documenting", ...)` + `grade_documenter(ctx) -> (contract_ok, markers, detail)` (checks `status == "docs_committed"` and wiki content present via `_docker_exec`) + rubric. Verdicts reuse the existing PASS/FAIL_MODEL/FAIL_HARNESS/ERROR taxonomy. See `contracts/benchmark.md`.
