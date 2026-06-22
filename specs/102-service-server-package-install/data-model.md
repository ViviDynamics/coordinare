# Data Model: Env-Bootstrap Delivers Runnable Service Server Binaries

**No new store, no schema change.** The change is to the rendered bootstrap
instruction (the deb-fetch command); the kind→package mapping is unchanged.

## 1. Service-kind → package mapping (existing, unchanged)

| Kind | Packages (`_SERVICE_KIND_PACKAGES`) | Note |
|---|---|---|
| `postgres` | `("postgresql", "postgresql-client")` | `postgresql` is the version-agnostic META handle; apt resolves it to the current `postgresql-NN` server when the fetch resolves the closure |
| `redis` | `("redis-server",)` | already a concrete server package |

The mapping stays the **single source of truth** for which packages a kind needs (FR-005); the bug was the *fetch*, not the names.

## 2. The fetch command (changed)

| Before (fragile) | After (robust, closure-resolving) |
|---|---|
| `apt-get download $(apt-cache depends --recurse … -i <pkgs> \| grep '^\w' \| sort -u)` | `apt-get install -y --download-only -o Dir::Cache::archives=<cache>/debs/ <pkgs>` |

Output: the full dependency closure (meta → versioned server → deps) as `.deb`s in `<cache>/debs/`, then `dpkg-deb -x` extracted onto the cache PATH (unchanged delivery). **No hard-pinned version** — apt resolves the current distro's server (FR-004).

## 3. Resolved closure (transient; what must result)

For `postgres`, after the fetch `<cache>/debs/` MUST contain the versioned server package (e.g. `postgresql-NN_..._<arch>.deb`) whose extraction yields runnable `initdb` / `pg_ctl` / `postgres` — the binaries spec-101's `run_service_readiness` then starts + health-checks. (This spec makes 101's gate PASS for a correctly-declared service.)

## 4. Reused (unchanged)

- `derive_service_install_items` (091) — derives the `kind="system"` ManifestItems from the mapping.
- env-cache deb extraction + activate.sh PATH auto-discovery.
- spec-101 `run_service_readiness` — the runtime verifier/backstop.
