# Contract: Env-Bootstrap Service Server-Binary Install

Replaces the fragile service-deb fetch in the bootstrap persona with a robust,
closure-resolving, version-resilient download. Mapping + delivery + the 101 gate
are reused.

## Rendered-instruction contract (`_render_system_services_install`)

| Aspect | Requirement |
|---|---|
| Fetch command | Uses `apt-get install … --download-only … -o Dir::Cache::archives=<cache>/debs/ <pkgs>` (apt resolves the full closure) — NOT `apt-get download $(apt-cache depends … \| grep)` |
| Packages | Names the kind's server + client packages from `_SERVICE_KIND_PACKAGES` (meta names OK — apt resolves them) |
| Version pin | NO hard-coded distro/package version (no `postgresql-17` literal) — apt resolves the current distro's server |
| Extraction | Still `dpkg-deb -x` each fetched `.deb` into the cache (binaries on PATH via activate.sh) |
| No services | Empty block when no stateful services declared (byte-for-byte unchanged) |

## Invariants (MUST)

1. **Server binaries delivered (FR-001/FR-002, SC-001):** the fetch resolves a meta-package's closure so the versioned server (with `initdb`/`pg_ctl`/`postgres`) lands in `<cache>/debs/`, not just the meta + client.
2. **Passes the 101 gate (FR-003, SC-002):** a correctly-declared service, after this install, has runnable binaries → spec-101 readiness gate starts + connects → bootstrap completes.
3. **Version-resilient (FR-004, SC-003):** no brittle hard-pinned version; apt resolves the current distro's server.
4. **General (FR-005, SC-004):** the shared command renders for any coordinare-known kind; no per-kind special-casing.
5. **Fail-clear (FR-006, SC-005):** an unobtainable server package fails the fetch → 101 blocks; never a silent meta/client-only install marked complete.
6. **Secret-free / no new dep / image stays service-agnostic (FR-007):** no secrets in the instruction/logs; reuse apt + the existing delivery; binaries ride the cache.

## Example rendered fetch (postgres)

```
cd <cache>/debs && apt-get update \
  && apt-get install -y --download-only -o Dir::Cache::archives=<cache>/debs/ \
       postgresql postgresql-client \
  && for d in <cache>/debs/*.deb; do dpkg-deb -x "$d" <cache>/services-extract; done
```
(apt resolves `postgresql` → `postgresql-NN` server + deps; binaries land in
`<cache>/services-extract/usr/bin` and activate.sh auto-discovers that `*/usr/bin`
onto PATH. The extraction target is a concrete dir — no agent-filled `<prefix>`
placeholder that could be taken literally.)
