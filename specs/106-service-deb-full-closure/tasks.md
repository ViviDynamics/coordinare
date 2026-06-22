# Tasks: Service-Deb Fetch Full Runtime-Lib Closure

**Tests**: INCLUDED (Constitution II). **Additive to 102** (102's substring tests stay green).

## Phase 1: Setup
- [X] T001 Re-read `_render_system_services_install` (http_performer_service.py) current fetch command + its tests (`test_service_install_*`); `agent/performer/Dockerfile.base` apt block (snapshot insertion point after `rm -rf /var/lib/apt/lists/*`).

## Phase 2: US1 — runtime libs land regardless of container state (P1) MVP
- [X] T002 [US1] FAILING test in `test_http_performer_service.py`: the rendered service block resolves against the base-state snapshot — contains `Dir::State::status` and the snapshot path `/opt/coordinare-base-dpkg-status`, AND a fallback to `/var/lib/dpkg/status`. Keep all existing 102 assertions (apt-get install, --download-only, Dir::Cache::archives, dpkg-deb -x, no apt-cache depends). MUST fail first.
- [X] T003 [US1] In `_render_system_services_install`, add `_stat=/opt/coordinare-base-dpkg-status; [ -f "$_stat" ] || _stat=/var/lib/dpkg/status;` and `-o Dir::State::status="$_stat"` to the apt-get install line. Make T002 pass.
- [X] T004 [US1] In `agent/performer/Dockerfile.base`, after the apt-install block, add `RUN cp /var/lib/dpkg/status /opt/coordinare-base-dpkg-status`.

## Phase 3: US3 — fallback (P3)
- [X] T005 [US3] Test: the rendered block contains the `[ -f "$_stat" ] || _stat=/var/lib/dpkg/status` fallback so a missing snapshot degrades to live status. (Covered by T002; assert explicitly.)

## Phase 4: Polish
- [X] T006 [P] Full http_performer_service suite green; 102's tests still pass (additive change).
- [X] T007 `.venv/bin/ruff check`; rebuild :base/:full/:extra; empirically verify (docker) the fetch lands libicu76 with it pre-installed (approach D), and a clean `postgres --version` after extract + LD_LIBRARY_PATH.
- [X] T008 A couple of adversarial review rounds (diverse-lens + refute-verify): the Dir::State::status snapshot is correct + the fallback is safe; targeted set (no libc6 overlay); meta→server retained; secret-free; Dockerfile snapshot captured at the right layer.

## Implementation Strategy
MVP = US1 (snapshot + Dir::State::status). Additive to 102. Then rebuild image + re-bootstrap website to validate postgres actually starts (closes the live failure).
