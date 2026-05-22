# Phase 1 Data Model: Hermes Performer Backend

**Feature**: 068-hermes-backend | **Date**: 2026-05-21

The Hermes backend introduces no new persistent state and no
cross-process schemas. The "entities" below are the in-memory objects
inside `HermesBackend` and the on-disk artifacts created per job. They
exist for the lifetime of one performer job and are removed
unconditionally at terminal state (FR-011).

---

## E-1. HermesBackend (in-memory adapter)

Implements `BackendAdapter` Protocol from
`agent/performer/src/performer/backends/base.py`.

| Field | Type | Lifecycle | Notes |
|---|---|---|---|
| `_proc` | `asyncio.subprocess.Process \| None` | set in `start()`, cleared in `_finalize()` | The active `hermes chat` subprocess. `None` between turns when relay feedback re-invocation is pending. |
| `_profile_dir` | `pathlib.Path \| None` | created in `start()`, removed in `_finalize()` | Job-scoped `HERMES_HOME` (see E-2). |
| `_output_path` | `pathlib.Path` | created in `start()` | JSON output file Hermes writes on completion; lives inside `_profile_dir`. |
| `_status` | `BackendStatus` | mutated by `get_status()` / lifecycle hooks | One of `working`, `done`, `error`. Never a Hermes-internal state (FR-009). |
| `_events` | `list[BackendEvent]` | appended during run, drained by `drain_events()` | Same shape as peer backends. |
| `_feedback_queue` | `list[str]` | appended by `relay_feedback()`, consumed on next `start()`-equivalent invocation | One-shot replay pattern (R-004). |
| `_stand` / `_score` | references | captured at `start()` | Used to rebuild the prompt when relay feedback triggers re-invocation. |
| `_model` / `_effort` / `_temperature` / `_max_tokens` | optional overrides | captured at `start()` | Passed through to `hermes chat`. |
| `_stop_requested` | `bool` | set by `stop()` | Guards `_finalize()` against double-cleanup. |

### State transitions

```text
        start()             subprocess exit 0 + valid JSON
  ───►  working   ────────────────────────────────────────►  done  (terminal)
           │
           │  subprocess exit non-zero / empty / malformed / parse error
           ├───────────────────────────────────────────────►  error  (terminal)
           │
           │  stop()                          AGENT_TIMEOUT expiry
           └───────────────────────────────────────────────►  error  (terminal)
```

`relay_feedback()` does **not** transition state. It enqueues a string and
returns immediately. The next `start()`-equivalent re-invocation drains
the queue into the prompt (R-004).

### Validation rules

- `start()` MUST raise / set `error` if `HERMES_PROVIDER`,
  `HERMES_API_KEY`, or `HERMES_MODEL` env vars are missing (edge case:
  "credentials/model env are missing or invalid → fail fast at job
  start").
- `start()` MUST override any inherited `HERMES_HOME` from the parent
  process environment (FR-005).
- `get_status()` MUST NOT return any value outside
  `{working, done, error}` (FR-009).
- Any terminal transition MUST invoke `_finalize()` exactly once.

---

## E-2. HermesProfileDirectory (on-disk artifact)

A filesystem directory created per job at `start()` and removed at every
terminal outcome (FR-011).

| Property | Value |
|---|---|
| Location | `tempfile.mkdtemp(prefix="hermes-job-")` under the system temp dir |
| Exported as | `HERMES_HOME` env var to the `hermes chat` subprocess |
| Contents | Hermes' per-instance state (session db, scratch files, tool caches) + the adapter-written `hermes.config.yaml` carrying `disabled_toolsets` (R-002) + `_output_path` JSON file |
| Lifetime | One performer job; removed unconditionally on `done`, `error`, `stop`, or timeout |
| Isolation guarantees | Distinct path per job (SC-004); never points at `~/.hermes` (FR-005) |

---

## E-3. HermesCapabilitySet (config-time allow/deny list)

Not a runtime object — a static specification of which Hermes toolsets
the adapter enables. Encoded in two places per R-002:

| Surface | Value |
|---|---|
| CLI flag | `--toolsets terminal,file,search,browser,todo` |
| Profile config (`hermes.config.yaml` inside `HERMES_HOME`) | `disabled_toolsets: [gateway, messaging, cron, clarify, user_memory]` |

The set is constant for the MVP. Future changes (e.g., enabling
symphony-scoped memory) are an explicit follow-up and require updating
both surfaces atomically.

---

## Relationships

- One `HermesBackend` instance owns exactly one `HermesProfileDirectory`
  for its lifetime.
- One `HermesBackend` instance issues one or more `hermes chat`
  subprocess invocations; each invocation reads the same
  `HermesProfileDirectory` and applies the same `HermesCapabilitySet`.
- Concurrent `HermesBackend` instances on the same performer hold
  disjoint `HermesProfileDirectory` paths (SC-004 invariant).
