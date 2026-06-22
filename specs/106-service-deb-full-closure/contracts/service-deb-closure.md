# Contract: State-Independent Service-Deb Closure

## Field Registry
(No dispatch/payload field changes — rendered-instruction + image build only. Intentionally empty.)

## Rendered-fetch contract (`_render_system_services_install`)
| Aspect | Requirement |
|---|---|
| Resolution state | `-o Dir::State::status=<base-snapshot>` (pristine base dpkg status), with fallback to `/var/lib/dpkg/status` if absent |
| Closure | apt resolves meta→versioned server + the runtime-lib closure the BASE IMAGE lacks (e.g. libicu76), independent of container install state |
| Base OS libs | NOT re-fetched (already in base state) — targeted set, no LD_LIBRARY_PATH overlay |
| Cache dir | `-o Dir::Cache::archives=<cache>/debs/` (unchanged from 102) |
| Extraction | `dpkg-deb -x` each into `<cache>/services-extract` (unchanged from 102/the 105-era concrete dir) |
| Version pin | none (unchanged) |

## Image contract (Dockerfile.base)
| Aspect | Requirement |
|---|---|
| Snapshot | `cp /var/lib/dpkg/status /opt/coordinare-base-dpkg-status` after the base apt installs |
| Path | well-known, referenced by the rendered fetch; small status file (no binaries) |

## Invariants (MUST)
1. Runtime libs delivered regardless of container state (FR-002, SC-001): libicu76 lands in the cache even when pre-installed in the container.
2. Targeted, no base overlay (FR-003, SC-002): libc6 etc. not re-fetched.
3. Graceful fallback (FR-005, SC-003): missing snapshot → live status, no error.
4. 102 behavior retained (FR-004): meta→server, Dir::Cache::archives, dpkg-deb -x, no version pin.
5. Secret-free / no new dep / image service-agnostic (FR-006).
