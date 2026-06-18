# Contract: Readiness Checklist (`verify.sh`)

The coordinare-owned `verify.sh`, rendered by `env_manifest.render_verify_sh(manifest, *,
cache_mount_path)`, is a manifest-driven readiness checklist. It runs inside a clean
consumer-context container with the env-cache mounted **read-only** (no bootstrap helpers, no
operational secrets injected). Each declared manifest item emits exactly one classification line;
the aggregate process exit code drives the dispatch gate.

## Line classification contract

Each line begins with one of three tokens followed by the item name/path and a human-readable
reason (FR-004):

| Token | Meaning | Contributes to nonzero exit? |
|-------|---------|------------------------------|
| `OK:` | Item present, correct version (if pinned), loadable/running as required | No |
| `FAIL:` | Item declared but not realized: missing binary, version mismatch, native extension not loadable, or coordinare-managed service installed-but-not-running/unhealthy | **Yes** |
| `WARN:` | Test/qa-only nicety absent (browser on PATH, client utility); non-blocking | No |

Aggregate: process exits **nonzero iff at least one `FAIL:` line was emitted**, else `0`.

## Probe semantics by manifest item kind

| Item kind | Probe (emitted into shell, toolchain-specific) | Classification rule |
|-----------|-----------------------------------------------|---------------------|
| `runtime` | resolve binary via sourced `activate.sh`; compare reported version to pinned `version` | resolvable + version match ⇒ `OK`; missing or version mismatch ⇒ `FAIL` |
| `gem` | `bundle show <name>` / `gem list -i <name>` under the project dependency manifest | present ⇒ `OK`; absent ⇒ `FAIL` |
| native extension (rails/psych boot) | `BUNDLE_GEMFILE=<gemfile> bundle exec ruby -e "require '<ext>'"` smoke test, emitted when a gem/rails item exists | loads ⇒ `OK`; raises ⇒ `FAIL` |
| coordinare-managed service (`postgres`) | `pg_isready` against the managed socket/port | accepting connections ⇒ `OK`; installed-but-not-running/refused ⇒ `FAIL` |
| coordinare-managed service (`redis`) | `redis-cli ... PING` expecting `PONG` | `PONG` ⇒ `OK`; not running/no `PONG` ⇒ `FAIL` |
| `system` / `node_pkg` (qa-only niceties) | presence on PATH | present ⇒ `OK`; absent ⇒ `WARN` (never `FAIL`) |

`runtime`, `gem`, and native-extension classification is **unchanged** from the current
`render_verify_sh` / `_default_check`. `system` / `node_pkg` already emit non-blocking `WARN`. The
**net-new** behavior for spec 093 is the coordinare-managed-service RUNNING/healthy line at `FAIL`
level (installed-but-not-running ⇒ `FAIL`, not `WARN`).

## Invariants

- **Toolchain-agnostic (NON-NEGOTIABLE)**: coordinare Python only selects a probe *kind* from
  `ManifestItem.kind`; the actual version-manager/health command (`pg_isready`, version flags) is
  emitted into the shell. No rbenv/nvm/asdf knowledge in coordinare Python (FR-010).
- **Secret invariant (NON-NEGOTIABLE)**: only env-var NAMES and file PATHS appear in lines; service
  health probes needing a password use the redacted secrets channel (`--pwfile=<(...)`-style),
  never echoing the value (FR-009). All interpolated tokens pass through the `shq` Jinja filter.
- **Idempotent / side-effect-free**: probes observe state (resolve, version, load, health); they do
  not install, migrate, or mutate the read-only-mounted cache.

## Field Registry

Fields/identifiers this contract governs. (Used by `/speckit.analyze` Contract Check. No new
dispatch-payload fields are introduced by this feature; entries below are the existing manifest
fields and shell line tokens the checklist reads/emits.)

| Field | Type | Owner | Notes |
|-------|------|-------|-------|
| `name` | `str` | `ManifestItem` | Item NAME only; emitted in line text via `shq`. Never a secret. |
| `kind` | `"runtime" \| "gem" \| "system" \| "node_pkg"` | `ManifestItem` | Selects probe kind; service kinds (postgres/redis) carried on service descriptor. |
| `version` | `str \| None` | `ManifestItem` | Version-match source for `runtime`; `None` ⇒ presence-only. |
| `check` | `str \| None` | `ManifestItem` | Explicit shell probe; `None` ⇒ `_default_check`. Toolchain-specific shell lives here. |
| `cache_mount_path` | `str` | `render_verify_sh` kwarg | Read-only mount path inside the consumer container. |
| `OK` | shell line token | `verify.sh` | Non-blocking; item realized. |
| `FAIL` | shell line token | `verify.sh` | Blocking; drives nonzero aggregate exit. |
| `WARN` | shell line token | `verify.sh` | Non-blocking; qa-only nicety absent. |
