# Research: Env-Bootstrap Delivers Runnable Service Server Binaries

## D1 — Why the current fetch dropped the versioned server

The persona instructs:
```
apt-get download $(apt-cache depends --recurse --no-recommends --no-suggests \
  --no-conflicts --no-breaks --no-replaces --no-enhances -i postgresql postgresql-client \
  | grep '^\w' | sort -u)
```
Two fragilities dropped `postgresql-NN`:
- **`-i` (important-only)** restricts `apt-cache depends` to Depends/Pre-Depends; combined with the other `--no-*` filters and meta-package quirks, the recursion into the versioned server can be lost.
- **`grep '^\w'`** keeps only lines starting at column 0 (package headers). `apt-cache depends` prints the recursed package on its own col-0 line, but the parsing is brittle across apt versions/output formats, and a header like `postgresql-17` may be filtered or never emitted if `--recurse` didn't follow into it.

Result: only the arch-independent **meta** debs (`postgresql`, `postgresql-client`) + a directly-named lib (`libpq5`) were fetched — no `initdb`/`pg_ctl`/`postgres`.

## D2 — Robust replacement: `apt-get install --download-only` (closure-resolving)

**Decision**: Replace the `download $(apt-cache depends … | grep)` pipeline with:
```
apt-get update && apt-get install -y --download-only \
  -o Dir::Cache::archives=<cache>/debs/ <server-pkg> <client-pkg>
```
`apt-get install --download-only` runs apt's **real dependency resolver**: it computes the full install set for the named packages (meta → its versioned server dependency → that server's deps) and downloads **all** of them as `.deb`s into the archives cache (`<cache>/debs/`), without installing into the live system. The agent then `dpkg-deb -x` extracts them (as today) so the binaries land on PATH via activate.sh.

**Rationale**: apt's resolver is the authoritative, robust way to expand a meta-package to its concrete server — no hand-rolled `apt-cache depends | grep` parsing. It fetches exactly the closure needed to run the service.

**Alternatives rejected**:
- Hardening the `apt-cache depends | grep` pipeline — still brittle across apt output formats; reuse the resolver instead.
- A separate `apt-get download` of the full `apt-cache depends --recurse` list without `-i`/grep — closer, but `--download-only install` is the canonical, simplest closure fetch.

## D3 — Version resilience: keep the meta name, let apt resolve

**Decision**: Keep `_SERVICE_KIND_PACKAGES` naming the **meta** package (`postgres → ("postgresql","postgresql-client")`). apt resolves `postgresql` to whatever versioned server the **current** base-image distro provides (`postgresql-17` today, `-18` later) — so there's NO brittle hard-coded version (FR-004). Hard-coding `postgresql-17` is explicitly rejected: it silently breaks when the base image's distro advances.

**Rationale**: the meta-package *is* the version-agnostic handle; the only bug was not resolving its closure. Fixing the fetch (D2) makes the meta name correct + future-proof.

## D4 — Agent-executed instruction vs deterministic step

**Decision**: Keep the fetch **agent-executed** (the rendered persona instruction), but with the robust D2 command. Spec-101's readiness gate is the backstop: if the binaries still don't materialize, the bootstrap **fails loudly** (not a silent broken cache). A fully **deterministic** coordinare-owned fetch (removing the agent from the critical path) is a larger change deferred to a follow-up — the robust command + the 101 gate is the focused, sufficient fix for the observed failure.

**Rationale**: the observed failure was a fragile *command*, not solely agent non-determinism; a robust command + the 101 safety net closes the gap with a minimal, testable change. Determinism can follow if agent-reliability proves insufficient even with the robust command.

## D5 — Generality (FR-005)

**Decision**: The fix is in the shared fetch command (`_render_system_services_install`) which renders for ANY coordinare-known kind's package set. `redis → ("redis-server",)` (already a concrete server package) and any future kind benefit equally — a meta-named kind gets its closure resolved; a concrete-named kind downloads as-is plus deps. No per-kind special-casing.
