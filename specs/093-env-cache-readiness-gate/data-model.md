# Phase 1 Data Model: Env-Cache Toolchain-Readiness Dispatch Gate

This feature introduces **no new persisted coordinare state** and **no new pydantic models**. It
reuses existing entities and changes the *behavior* attached to them (how the dispatch guard reads
readiness; what `verify.sh` probes). The entities below are the existing structures this feature
reads, plus the transient readiness verdict that flows through the dispatch decision and is never
persisted.

## Entity 1 — Env manifest (`EnvManifest` / `ManifestItem`)

Source: `src/coordinare/models/env_manifest.py`. Authoritative declaration of what the env-cache
must realize for a given spec sha. Derived from `.coordinare/score.json` manual override (trusted)
or LLM inference. **Read-only for this feature** — 093 hardens *realization* of these items, not
their *completeness*.

### `EnvManifest`

| Field | Type | Notes |
|-------|------|-------|
| `symphony_name` | `str` | Symphony this manifest belongs to. |
| `items` | `list[ManifestItem]` | Declared toolchain/services/extensions. Drives the checklist. |
| `spec_sha` | `str \| None` | The spec sha this manifest was built for — the readiness gate is keyed to the **current** spec sha. |
| `llm_derived` | `bool` | True when from inference, False when from score.json override. |

Method `runtime_pins()` → the version-pinned runtimes used for version-match probes.

### `ManifestItem`

| Field | Type | Validation | Notes |
|-------|------|-----------|-------|
| `name` | `str` | non-empty | Binary/package/service name (NAME only — never a secret). |
| `kind` | `ItemKind` = `"runtime" \| "gem" \| "system" \| "node_pkg"` | literal | Selects the probe *kind* the templater emits. |
| `version` | `str \| None` | non-empty/no-NUL when set; `None` allowed | Drives version-match FAIL for `runtime`. |
| `source` | `str` | — | Provenance (diagnostic). |
| `check` | `str \| None` | `None` ⇒ use `_default_check(item)` | Optional explicit shell probe; toolchain-specific shell lives HERE, not in Python. |
| `install_hint` | `str` | — | Diagnostic only. |

**Service kinds** (`postgres`, `redis`) are carried on the service-descriptor side (spec-091),
mapped to client/health semantics by the service-kind helpers in `env_manifest.py` (~156-174):
postgres → `pg_isready`, redis → `redis-server` / `PING`. These are the coordinare-managed services
whose **RUNNING/healthy** state US2 adds as a FAIL-level checklist line.

## Entity 2 — Readiness checklist (`verify.sh`, rendered by `render_verify_sh`)

Source: `src/coordinare/services/env_manifest.py:render_verify_sh` (~318-361). A coordinare-owned
POSIX/bash script generated from the manifest via Jinja2 (`StrictUndefined`, `shq` shell-quote
filter). It is the *physical embodiment* of the readiness contract — see
[contracts/verify-sh-checklist.md](contracts/verify-sh-checklist.md).

| Aspect | Current behavior | 093 change |
|--------|------------------|------------|
| Sources `activate.sh` | yes | unchanged |
| `runtime` line | binary resolvable + version match ⇒ FAIL on mismatch | unchanged |
| `gem` line | `bundle show` / `gem list -i` presence | unchanged |
| native-extension boot | rails/psych `bundle exec ruby -e "require ..."` smoke test when a gem/rails item exists | unchanged |
| `system` / `node_pkg` line | soft `WARN: <name> not on PATH (test/qa-only; non-blocking)` | unchanged (these are the niceties) |
| **coordinare-managed service line** | **absent** | **NEW: postgres `pg_isready` / redis `PING` ⇒ RUNNING/healthy or FAIL (installed-but-not-running ⇒ FAIL)** |
| Aggregate | exit 1 iff any `FAILED`, else 0 | unchanged |

Per-line output is one `OK:` / `FAIL:` / `WARN:` token (FR-004). Only `FAIL:` contributes to the
nonzero aggregate exit; `WARN:` never does (FR-003). All interpolated names/paths pass through
`shq`; no secret values are emitted (FR-009).

## Entity 3 — Readiness result (tri-state, transient)

Source: `daemon._verify_env_cache_clean(symphony, svc)` (~1738-1812). Runs `verify.sh` in a
clean consumer-context container (`docker run --rm -v {cache_dir}:{path}:ro --entrypoint bash`,
300s timeout) and returns a tri-state. **Not persisted** — recomputed every dispatch (FR-006).

| Value | Meaning | Gate effect |
|-------|---------|-------------|
| `True` | `verify.sh` exited 0 | READY — dispatch proceeds |
| `False` | `verify.sh` exited nonzero (detail captured) | NOT READY — kick to env-bootstrap |
| `None` | `verify.sh` absent | DEGRADED — **MUST NOT block** (FR-005); dispatch proceeds on legacy `last_bootstrap_succeeded` |

This tri-state contract is pinned by existing tests in `test_daemon_snapshot_persistence.py` and
**must remain unchanged** (Constitution II obligation).

## Entity 4 — Attempt budget (`env_bootstrap_max_attempts` + `EnvCacheState` counters)

Source: `config.py:937` (`env_bootstrap_max_attempts`, default 3, `ge=1`, `le=20`) and the
existing `EnvCacheState` bootstrap-attempt counters managed by
`EnvCacheService.check_and_trigger` / `on_bootstrap_complete`. **No new field, no new knob**
(FR-008).

| Field | Source | Notes |
|-------|--------|-------|
| `env_bootstrap_max_attempts` | `config.py:937` | Upper bound on the dispatch→bootstrap→dispatch loop. |
| bootstrap attempt counter | `EnvCacheState` | Incremented by `check_and_trigger`; reset on spec-sha change. |
| `bootstrap_exhausted` | derived at `>= max` | Surfaces the actionable env-blocked verdict (no new readiness-specific counter). |

State transition on a `False` readiness result for a code-running dispatch:

```text
dispatch (code-running stage)
  └─ _verify_env_cache_clean == False
       └─ check_and_trigger(current spec sha)   # existing re-bootstrap
            ├─ attempts < max  → bootstrap_in_flight hold → re-dispatch → re-verify
            └─ attempts >= max → bootstrap_exhausted → env-blocked verdict (actionable, no thrash)
```

`None` (degraded) and `True` (ready) do not enter this transition. Spec-sha change resets the
budget via the existing reset path, so a fresh sha gets a full attempt budget.

## Relationships

```text
EnvManifest ──(render_verify_sh)──▶ verify.sh ──(_verify_env_cache_clean exec)──▶ tri-state result
                                                                                       │
                                                          ┌────────────────────────────┤
                                                          ▼ True                ▼ False │ ▼ None
                                                     dispatch              check_and_trigger  dispatch
                                                     proceeds            (budget-bounded loop) proceeds
                                                                                              (legacy)
```

The gate lives at the existing dispatch readiness guard in `dispatch_performer.py` (~973-1000),
applied to the code-running stages enumerated by `ROLE_TO_STAGE` in `lifecycle.py`; `env_bootstrap`
is exempt.
