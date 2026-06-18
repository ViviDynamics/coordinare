# Phase 0 Research: Env-Cache Toolchain-Readiness Dispatch Gate

All Technical Context items are resolved against existing, named seams — this feature is an
extension of the spec-091/092 env-cache line, not a greenfield capability. There were no
`NEEDS CLARIFICATION` markers. The decisions below pin which existing contract each requirement
attaches to and why the alternative was rejected.

## Decision 1 — Gate at the existing dispatch readiness guard, not a new node

**Decision**: Extend the readiness check inside `dispatch_performer.py` (the `_current_and_verified`
guard at ~lines 973-1000) so that, for code-running stages, "verified" additionally requires a
passing `verify.sh` readiness run. Keep the existing `env_cache_not_current` /
`bootstrap_in_flight` hold as the single hold point.

**Rationale**: The guard already withholds dispatch and releases the slot for not-current caches
(lines 986-1000). Layering readiness onto the same decision means one hold path, one log family
(`dispatch_performer.env_cache_not_current`), and no new branch in the graph. The set of
code-running stages is already enumerated by `ROLE_TO_STAGE` in `lifecycle.py` (qa, implementing,
reviewing, plus security/documenting/architecting/advocate/assessing/closing_review);
`env_bootstrap` stays exempt via the existing `_is_bootstrap_dispatch` check.

**Alternatives rejected**:
- *A new readiness graph node* — duplicates the slot-release/hold logic and creates a second
  place dispatch can be withheld; rejected for divergence risk.
- *Gate in the daemon poll loop* — too coarse; the per-card dispatch decision is the correct
  granularity and already has the cache context in hand.

## Decision 2 — Reuse `_verify_env_cache_clean` tri-state, re-run each dispatch

**Decision**: Invoke the existing `daemon._verify_env_cache_clean(symphony, svc)` on each
code-running dispatch. It runs `verify.sh` in a clean consumer-context container
(`docker run --rm -v {cache_dir}:{container_path}:ro --entrypoint bash`, 300s timeout) and
returns `True` (exit 0), `False` (nonzero), or `None` (`verify.sh` absent). `True` ⇒ ready;
`False` ⇒ FAIL (kick to bootstrap); `None` ⇒ degraded, MUST NOT block.

**Rationale**: This seam is already proven and unit-pinned in
`test_daemon_snapshot_persistence.py` (None when absent, True on 0, False+detail on nonzero). The
clean consumer-context (read-only mount, no bootstrap helpers) is exactly the "would a real
performer see a working env?" question. Re-running each dispatch (FR-006) is a property of where
we call it (the dispatch decision), not of any cache field — no persisted readiness state is
introduced, satisfying the "no cross-dispatch caching" out-of-scope boundary.

**Alternatives rejected**:
- *Persist the last readiness verdict on `EnvCacheState`* — directly violates FR-006 / the
  Out-of-Scope "caching readiness results across dispatches"; a stale pass is the bug we are
  closing. Rejected.
- *A bespoke readiness exec separate from `_verify_env_cache_clean`* — would fork the tri-state
  contract and the container-exec plumbing for no benefit.

## Decision 3 — Checklist semantics live in `render_verify_sh`, derived from the manifest

**Decision**: Enrich `env_manifest.render_verify_sh(manifest, *, cache_mount_path)` so each
declared `ManifestItem` emits an OK/FAIL/WARN line: (a) `runtime` kind ⇒ binary resolvable via
`activate.sh` AND version match (mismatch ⇒ FAIL); (b) native extension / `gem` kind ⇒ loadable
under the real project dependency manifest (not loadable ⇒ FAIL); (c) coordinare-managed service
(postgres/redis) ⇒ RUNNING/healthy via its health probe — `pg_isready` / `redis ... PING` —
installed-but-not-running ⇒ FAIL; (d) test/qa-only niceties (browser on PATH, client utility) ⇒
WARN, never driving the aggregate exit code. Aggregate exit code = nonzero iff any FAIL.

**Rationale**: Keeps coordinare Python toolchain-agnostic (FR-010) — the Python only iterates
manifest items and selects a probe *kind*; the actual shell probe (rbenv/nvm/version flags,
`pg_isready`) is emitted into `verify.sh`. The manifest already distinguishes `ItemKind`
(`runtime`/`gem`/`system`/`node_pkg`) and services already carry `kind` + health semantics from
spec-091. The `shq` Jinja filter prevents injection from interpolated names/paths.

**Alternatives rejected**:
- *Probe toolchains from coordinare Python (subprocess `ruby --version` etc.)* — the user
  explicitly called this "a mistake" (option A); it hardcodes version-manager knowledge into the
  orchestrator. Rejected, non-negotiable.
- *A single opaque "is it installed" check* — does not distinguish installed from running/usable,
  which is the precise gap (postgres installed-but-not-running, ruby present-but-wrong-version).

## Decision 4 — FAIL self-heals via the existing bootstrap trigger + budget

**Decision**: On a `False` readiness result for a code-running dispatch, route through the
existing `EnvCacheService.check_and_trigger` re-bootstrap for the current spec sha and fall into
the `bootstrap_in_flight` hold. Bound the loop with the existing
`env_bootstrap_max_attempts` (config.py:937, default 3); on exhaustion emit the existing
`env_cache.bootstrap_exhausted` path so the card surfaces an actionable env-blocked verdict.

**Rationale**: `check_and_trigger` / `on_bootstrap_complete` already implement attempt increment,
`bootstrap_exhausted` at `>= max`, and spec-sha-change budget reset. Reusing them means no new
budget knob (FR-008) and the FAIL path looks identical to the existing not-current path from the
card's perspective. The spec-088 integrity gate remains the independent false-OK backstop for any
hollow pass that slips an incomplete manifest.

**Alternatives rejected**:
- *A new readiness-specific attempt counter* — duplicates `bootstrap_attempts` and risks the two
  diverging; the loop is the same dispatch→bootstrap→dispatch loop the budget already bounds.
- *Block indefinitely until manual intervention* — thrashing / no actionable verdict; rejected by
  FR-008 / SC-004.

## Decision 5 — Observability carries keys/paths only

**Decision**: Readiness decisions are emitted via structlog using env-var NAMES and file PATHS
only; `verify.sh` lines likewise name binaries/services/paths, never secret values. Service
health probes that need a password use the existing redacted secrets channel
(`--pwfile=<(...)`-style), never echoing the value.

**Rationale**: Carried verbatim from spec-091/092 (FR-009, non-negotiable). The clean-context
exec mounts the cache read-only and injects no operational secrets, so a readiness run has no
secret to leak by construction; the discipline is to keep it that way in the emitted lines.

**Alternatives rejected**: None — this is an inherited invariant, not a choice.

## Resolved unknowns

| Item | Resolution |
|------|------------|
| Which stages are "code-running"? | `ROLE_TO_STAGE` in `lifecycle.py`; `env_bootstrap` exempt. |
| Where does readiness run? | Existing `_verify_env_cache_clean` clean-context container exec. |
| New persisted state? | None — re-run each dispatch (FR-006). |
| New config knob? | None — reuse `env_bootstrap_max_attempts`. |
| Where does toolchain-specific probing live? | Generated `verify.sh` via `render_verify_sh`. |
| What bounds the self-heal loop? | Existing `env_bootstrap_max_attempts` + `bootstrap_exhausted`. |
