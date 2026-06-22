# Research: State-Independent Service-Deb Closure

## Decision: resolve the service-deb download against a build-time snapshot of the base image's dpkg status

`apt-get install --download-only -o Dir::State::status=<base-snapshot> -o Dir::Cache::archives=<cache>/debs/ <pkgs>`, where `<base-snapshot>` is a copy of `/var/lib/dpkg/status` taken in `Dockerfile.base`.

## Empirical comparison (Debian 13 / python:3.12-slim, 2026-06-22; libicu76 pre-installed in the container to simulate the QA-browser install)

| Approach | libicu76 fetched? | libc6 fetched? | total pkgs | verdict |
|---|---|---|---|---|
| A. `--download-only install` (102 current, live state) | NO | no | ~62 | BROKEN — skips libicu76 already in container |
| B. `apt-cache depends --recurse \| apt-get download` | YES | yes | ~170 | works but full OS closure + virtual-pkg pitfalls |
| C. `--download-only install -o Dir::State::status=/dev/null` | YES | yes | 172 (114MB) | works but full OS closure; risks libc/systemd overlay on LD_LIBRARY_PATH |
| **D. `--download-only install -o Dir::State::status=<base-snapshot>`** | **YES** | **no** | **63 (45MB)** | **targeted + state-independent — chosen** |

## Rationale
- D fetches exactly the packages the *base image* lacks (the service's runtime libs incl libicu76) while skipping the OS libs (libc6/libssl) that already ship — targeted, and avoids overlaying base libraries onto LD_LIBRARY_PATH.
- It is state-independent: libicu76 is fetched whether or not the bootstrap container already installed it.
- It keeps apt's resolver (meta→versioned-server, virtual-package handling) — unlike raw `apt-get download`.
- Snapshot lives in Dockerfile.base (the immutable OS layer); a fallback to `/var/lib/dpkg/status` keeps older images working.

## Root cause (confirmed live)
postgres failed at QA with `error while loading shared libraries: libicui18n.so.76`. The base image has no libicu; 102's `--download-only` skipped it because an earlier bootstrap step had installed it into the container; the ephemeral container was discarded so it never reached the persistent cache.
