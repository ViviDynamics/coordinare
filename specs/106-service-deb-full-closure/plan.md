# Implementation Plan: Service-Deb Fetch Full Runtime-Lib Closure

**Branch**: `106-service-deb-full-closure` | **Spec**: [spec.md](spec.md)

## Technical Context
**Language**: Python 3.14 host-side render emitting bash run in the Debian performer; Dockerfile (apt/dpkg).
**Primary deps**: `_render_system_services_install` (`src/coordinare/services/http_performer_service.py`); `agent/performer/Dockerfile.base`. No new external dep.
**Testing**: `.venv/bin/pytest tests/unit/services/test_http_performer_service.py`; TDD. Plus an empirically-validated docker check (manual, recorded in spec).
**Scope**: (1) Dockerfile.base snapshots `/var/lib/dpkg/status`; (2) the rendered fetch adds `-o Dir::State::status=<snapshot>` with a missing-file fallback. Purely additive to 102's command.

## Constitution Check
- TDD: failing test first (persona contains `Dir::State::status` + fallback) → impl. PASS.
- Secret invariant: generic command, no values. PASS.
- No new dep; image stays service-agnostic (small status file). PASS.
- Additive to 102 (102's substring tests stay green). PASS.

## Design

**Dockerfile.base** — after the apt-install block (the `rm -rf /var/lib/apt/lists/*` line), add:
```dockerfile
# spec-106: snapshot the pristine base installed-set so the env-bootstrap service-deb
# fetch can resolve "what the base image lacks" (e.g. libicu76) independent of whatever
# the bootstrap container later installs.
RUN cp /var/lib/dpkg/status /opt/coordinare-base-dpkg-status
```

**`_render_system_services_install`** — change the fetch command to resolve against the snapshot, with a fallback when the file is absent (older images):
```
cd {cache}/debs && apt-get update && \
  _stat=/opt/coordinare-base-dpkg-status; [ -f "$_stat" ] || _stat=/var/lib/dpkg/status; \
  apt-get install -y --download-only -o Dir::State::status="$_stat" \
    -o Dir::Cache::archives={cache}/debs/ {pkgs}
```
(then the existing `for d in {cache}/debs/*.deb; do dpkg-deb -x ... {cache}/services-extract; done`)

### Why base-state resolution (vs alternatives)
- `--download-only install` against the live container state SKIPS deps already installed by earlier bootstrap steps (the libicu76 bug).
- `Dir::State::status=/dev/null` (empty state) fetches the ENTIRE OS closure (~172 pkgs/114MB incl libc6/systemd) — wasteful and risks overlaying base libs on LD_LIBRARY_PATH.
- `apt-cache depends --recurse | apt-get download` is also full-closure (~same) and has virtual-package pitfalls.
- **Base-state snapshot** yields the targeted set (63 pkgs incl libicu76, excl libc6) and is state-independent — empirically verified 2026-06-22.

## Phase 0 — research.md (the A/B/C/D empirical comparison)
## Phase 1 — contracts/ + quickstart
## Phase 2 — tasks.md
