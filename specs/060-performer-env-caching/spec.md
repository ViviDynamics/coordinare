# Feature Specification: Performer Environment Caching

**Feature Branch**: `060-performer-env-caching`
**Created**: 2026-05-08
**Status**: Draft
**Input**: User description: "Run a performer every time the README.md of the symphony's project changes, to set up or modify the performer's developer environment (languages, packages, tools). Cache the result outside the container, specific to the symphony. When a performer is dispatched for a symphony, the coordinare ensures the cached env volume is mounted and accessible."

## Clarifications

### Session 2026-05-08

- Q: What is the single file whose changes trigger an env-bootstrap run? → A: `README.md` at the root of each symphony's repository (the same file already tracked by `doc_sources[0]`).
- Q: Should env-bootstrap block regular performer dispatches while it is running? → A: No. Regular dispatches proceed normally. If no cache exists yet, performers run without a mounted env volume; once the bootstrap completes the volume is available for subsequent dispatches.
- Q: Which performer persona/role executes the env-bootstrap? → A: A new dedicated role: `env_bootstrap`. This role is always containerized (mode: ephemeral or persistent). It is configured per-symphony or at the global level, similarly to other performer registrations.
- Q: What does the `env_bootstrap` performer actually do? → A: It reads the README content (provided in the job payload), detects required runtimes and tools, installs them into the mounted `/devenv` volume path, and exits. The concrete implementation is performer-side; coordinare only dispatches and tracks completion.
- Q: Where is the cache stored on the host? → A: `{env_cache_root}/{symphony_name}/` on the coordinare host. `env_cache_root` is a new global config field, defaulting to `~/.coordinare/env-caches/`.
- Q: How is README change detected? → A: Coordinare fetches the file's Git blob SHA from the GitHub API each poll cycle per symphony. If the SHA differs from the last known value (stored in `CoordinareState`), a bootstrap dispatch is triggered.
- Q: How is the env cache volume mounted for regular performers? → A: Read-only at `/devenv` inside the container. The `env_bootstrap` performer mounts it read-write at the same path.
- Q: What happens if the env_bootstrap performer is not configured for a symphony? → A: Env-caching is disabled for that symphony. No volume is mounted on regular performers and no README tracking occurs.

### Session 2026-05-08 (follow-up — activation and persistent mode)

- Q: How does a performer actually activate the cached environment when a job starts? → A: Coordinare includes `env_cache_path` in the job init payload's `metadata` field (e.g. `"/devenv/my-symphony-abc123"`). The performer image is responsible for sourcing or activating the environment at that path (e.g. `source /devenv/my-symphony-abc123/activate`, or adding `/devenv/my-symphony-abc123/bin` to `PATH`). Coordinare defines the path; the performer image defines how to use it.
- Q: Should the mount point be `/devenv/` root or a named subdirectory? → A: Named subdirectory: `{container_devenv_root}/{sanitised_name}/`. Bootstrap installs into that subdirectory (rw); regular performers receive it read-only at the same path. This makes ephemeral and persistent modes consistent — a persistent performer can have multiple symphonies mounted under `/devenv/` with no path collision.
- Q: How do persistent performers handle multiple symphonies? → A: At container start, coordinare mounts all symphony env caches that are `cache_dir_ready` under their respective `/devenv/<sanitised_name>/` paths. When a job arrives with `env_cache_path` set, the performer activates the correct subdirectory. No restart or dynamic remounting is required.
- Q: How is persistent performer state reset between jobs? → A: The performer HTTP protocol gains a `POST /reset` endpoint. Coordinare calls it after a job completes and before dispatching the next one to the same persistent performer. The performer is responsible for clearing working directories, temp files, and any in-process state. Coordinare logs a warning and continues if `/reset` is not implemented (404) — the endpoint is optional for backwards compatibility.
- Q: What is `container_devenv_root`? → A: A new config field on `PerformerEndpointConfig` (default `/devenv`) giving operators control over the in-container path prefix. The path is passed back to the performer in `metadata.env_cache_path` as the full resolved path including the sanitised symphony name suffix.

---

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Operator Provisions a Fresh Environment on First Start (Priority: P1)

An operator configures a symphony and sets up an `env_bootstrap` performer pointing at a Docker image that knows how to install developer tools. When coordinare starts and polls the symphony for the first time, it detects that no cached env exists (no prior README SHA), fetches the README, and immediately dispatches the `env_bootstrap` performer. The performer installs languages and packages into the mounted host volume. On subsequent dispatches to regular performers, the env volume is automatically mounted read-only, giving performers the tools they need without re-running setup.

**Why this priority**: This is the core value of the feature. Without an initial bootstrap, the env cache never exists and no performer benefits from it.

**Independent Test**: Configure a symphony with an `env_bootstrap` performer. Start coordinare. Observe a bootstrap dispatch on the first cycle. Confirm a directory is created at `{env_cache_root}/{symphony_name}/`. Dispatch a regular performer on the next cycle and confirm `/devenv` is mounted inside its container.

**Acceptance Scenarios**:

1. **Given** a symphony with `env_bootstrap` configured and no prior README SHA, **When** coordinare starts and polls, **Then** an `env_bootstrap` dispatch is triggered on the first cycle with the README content in the job payload.
2. **Given** the `env_bootstrap` performer completes successfully, **When** the next regular performer is dispatched for that symphony, **Then** the env cache volume is mounted read-only at `/devenv` inside the container.
3. **Given** the env_bootstrap is running, **When** a regular performer is also dispatched, **Then** the regular performer dispatch proceeds normally (env volume absent until bootstrap completes if not yet present on disk).
4. **Given** `env_bootstrap` is not configured for a symphony, **When** a regular performer is dispatched, **Then** no `/devenv` volume is mounted and no README SHA tracking occurs.

---

### User Story 2 — Environment is Automatically Refreshed When README Changes (Priority: P1)

An operator updates the README to add a new language requirement. On the next coordinare poll cycle, coordinare detects the README blob SHA has changed, and dispatches a new `env_bootstrap` performer. The performer updates the cached volume. Subsequent performer dispatches automatically benefit from the refreshed environment without any manual intervention.

**Why this priority**: Without change detection, the cache becomes stale and operators must manually trigger re-provisioning.

**Independent Test**: Record the current README SHA. Update the README in the GitHub repo. Wait for a coordinare poll cycle. Observe a new `env_bootstrap` dispatch. Confirm the `env_readme_sha` in `CoordinareState` is updated to the new SHA.

**Acceptance Scenarios**:

1. **Given** coordinare has a stored README SHA for a symphony, **When** the remote README blob SHA changes on the next poll, **Then** a new `env_bootstrap` dispatch is triggered within one cycle.
2. **Given** the README has not changed, **When** coordinare polls, **Then** no `env_bootstrap` dispatch is triggered.
3. **Given** the GitHub README fetch fails (API error, network issue), **When** coordinare polls, **Then** no bootstrap dispatch occurs and a warning is logged; the last known SHA is preserved.
4. **Given** a bootstrap is already in flight (dispatched but not yet complete), **When** another README change is detected, **Then** the new bootstrap is queued and dispatched once the in-flight one completes (no concurrent bootstraps for the same symphony).

---

### User Story 3 — Each Symphony Has Its Own Isolated Cache (Priority: P2)

An operator runs two symphonies — a Python backend and a Node.js frontend. Each symphony has its own `env_bootstrap` performer and its own README describing different tooling requirements. Coordinare creates separate cache directories for each, installs different tool sets, and mounts the correct volume when dispatching performers for each symphony.

**Why this priority**: Without isolation, one symphony's bootstrap would overwrite another's environment.

**Independent Test**: Configure two symphonies with distinct READMEs. Run coordinare. Verify two separate directories under `{env_cache_root}/` are created (one per symphony name). Dispatch a performer for each symphony and confirm each receives the correct volume.

**Acceptance Scenarios**:

1. **Given** two symphonies each with `env_bootstrap` configured, **When** coordinare provisions both, **Then** two separate cache directories exist at `{env_cache_root}/{symphony_a_name}/` and `{env_cache_root}/{symphony_b_name}/`.
2. **Given** Symphony A's README changes, **When** coordinare polls, **Then** only Symphony A's bootstrap is re-triggered; Symphony B's cache is unaffected.
3. **Given** a regular performer is dispatched for Symphony A, **When** the container starts, **Then** it is mounted with Symphony A's volume (not Symphony B's).

---

### User Story 4 — Operator Controls Env Caching via Config (Priority: P3)

An operator can configure `env_cache_root` globally to point to a custom path. They can also enable or disable env caching per symphony by including or omitting the `env_bootstrap` performer. They can see bootstrap job status in the coordinare dashboard alongside regular performer sessions.

**Why this priority**: Operators must be able to direct where volumes land (e.g., on a mounted SSD) and see bootstrap activity without reading logs.

**Acceptance Scenarios**:

1. **Given** `env_cache_root: /mnt/fast-ssd/caches` is set in config, **When** coordinare provisions a symphony's environment, **Then** the cache directory is created at `/mnt/fast-ssd/caches/{symphony_name}/`.
2. **Given** an `env_bootstrap` dispatch is in flight, **When** the operator views the dashboard, **Then** the bootstrap session is listed with role `env_bootstrap` and the symphony name — distinct from regular card-work sessions.
3. **Given** the coordinare is restarted, **When** it starts up, **Then** it re-reads the stored README SHAs from `CoordinareState` (or re-fetches and compares) and only triggers a bootstrap if the README has changed since the last stored SHA.

---

### User Story 5 — Performer Activates Symphony Environment When Job Starts (Priority: P1)

A regular performer receives a job for a symphony that has an env cache. The job init payload includes `metadata.env_cache_path` pointing to the mounted subdirectory for that symphony (e.g. `/devenv/my-symphony-abc123`). The performer's entrypoint sources the activation script at that path, which sets `PATH`, `LD_LIBRARY_PATH`, and any other environment variables required to use the installed tools. The performer then executes the job using those tools without re-installing anything.

**Why this priority**: Without activation, the tools are present on disk but invisible to the performer process — the volume mount is inert without a PATH hook. This is the mechanism that makes the cache actually useful.

**Independent Test**: Configure a symphony with an env cache and dispatch a regular performer. Inspect the job init payload received by the performer and confirm `metadata.env_cache_path` is set to the correct subdirectory. Confirm the performer process can execute tools from that path without a fresh install.

**Acceptance Scenarios**:

1. **Given** a symphony with a ready env cache, **When** a regular performer is dispatched, **Then** the job init payload contains `metadata.env_cache_path` set to `{container_devenv_root}/{sanitised_name}`.
2. **Given** a symphony with no env cache configured, **When** a performer is dispatched, **Then** `metadata.env_cache_path` is absent from the payload.
3. **Given** a bootstrap performer is dispatched, **When** the job init payload is inspected, **Then** `metadata.env_cache_path` points to the same subdirectory with read-write access.

---

### User Story 6 — Persistent Performer Serves Multiple Symphonies Without Restart (Priority: P2)

A persistent performer is configured to handle jobs for multiple symphonies. At container start, coordinare mounts all ready symphony env caches as read-only subdirectories under `{container_devenv_root}/`. When a job arrives for any of those symphonies, the performer reads `metadata.env_cache_path` from the payload and activates only that symphony's environment — no remounting or restart required.

**Why this priority**: Persistent performers are long-lived containers. Without subdirectory-per-symphony mounting, a persistent performer can only serve one symphony's cached environment (the one baked into its startup config), which severely limits utilisation.

**Independent Test**: Configure a persistent performer with roles shared across two symphonies, both with ready env caches. Dispatch a job for symphony A — confirm `env_cache_path` points to symphony A's subdirectory. Dispatch a job for symphony B — confirm `env_cache_path` points to symphony B's subdirectory and the correct volume is already mounted.

**Acceptance Scenarios**:

1. **Given** a persistent performer and two symphonies both with ready env caches, **When** the performer container starts, **Then** both symphony cache dirs are mounted under `{container_devenv_root}/` as separate read-only subdirectories.
2. **Given** a job dispatched to the persistent performer for symphony A, **When** the payload is inspected, **Then** `metadata.env_cache_path` equals `{container_devenv_root}/{sanitised_name_A}`.
3. **Given** a new symphony's env cache becomes ready after the persistent performer is already running, **When** coordinare detects the ready state, **Then** the cache is NOT retroactively mounted (restart required); the operator is informed via a log warning.

---

### User Story 7 — Persistent Performer Resets State Between Jobs (Priority: P2)

After a persistent performer completes a job, coordinare calls `POST /reset` on the performer before dispatching the next job. The performer clears its working directory, temp files, and in-process caches, returning to a clean initial state. Subsequent jobs are not contaminated by artefacts from previous runs.

**Why this priority**: Without a reset step, a persistent performer accumulates state across jobs (leftover files, modified env vars, cached credentials). This creates non-deterministic behaviour that is hard to debug and can cause security cross-contamination between symphonies.

**Independent Test**: Run two sequential jobs on a persistent performer where job 1 creates a temp file at `/tmp/job-artefact`. Call `POST /reset`. Dispatch job 2 and confirm `/tmp/job-artefact` no longer exists inside the container.

**Acceptance Scenarios**:

1. **Given** a persistent performer that has just completed a job, **When** coordinare prepares to dispatch the next job, **Then** coordinare calls `POST /reset` on the performer before sending the new job init payload.
2. **Given** a performer that does not implement `POST /reset` (returns 404), **When** coordinare calls it, **Then** coordinare logs a warning and continues with dispatch — the endpoint is optional for backwards compatibility.
3. **Given** `POST /reset` returns a non-2xx error, **When** coordinare receives it, **Then** coordinare logs a warning and continues with dispatch (reset failure is non-fatal).

---

### Edge Cases

- What if the `env_cache_root` path does not exist? Coordinare creates it (and the symphony subdirectory) before mounting.
- What if the `env_bootstrap` container writes outside `/devenv`? The container is run with no extra capabilities; the host volume is scoped to the cache directory only.
- What if the env_bootstrap performer image is not pulled yet? The `docker run` failure is caught and logged; the next cycle retries.
- What if the symphony name contains characters invalid in directory names? Names are sanitised (alphanumeric + dash + underscore only) when forming the cache path.
- What if two coordinare instances share the same `env_cache_root`? Undefined behaviour; the operator is responsible for using distinct roots per coordinare instance.
- What if the README is very large? The blob SHA fetch is a metadata-only call; the full content is only fetched when a change is detected (to minimise API usage).
- What if a new symphony's env cache becomes ready after a persistent performer container has already started? The new cache cannot be retroactively mounted without a container restart; coordinare logs a warning. The operator must restart the persistent performer to pick up the new mount.
- What if the activation script at `env_cache_path` does not exist? That is a performer-image concern. Coordinare's responsibility ends at delivering the correct path in the payload.

---

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Coordinare MUST track the Git blob SHA of each symphony's README.md (or configured `env_spec_file`) per cycle and store it in `CoordinareState.env_cache[symphony_name].readme_sha` (type `str | None`, `None` = never fetched).
- **FR-002**: When the stored SHA is absent or differs from the fetched SHA, coordinare MUST dispatch an `env_bootstrap` performer for that symphony within the current poll cycle.
- **FR-003**: The `env_bootstrap` performer MUST receive the README content in the job payload.
- **FR-004**: Coordinare MUST mount the symphony's env cache volume read-write at `{container_devenv_root}/{sanitised_name}/` inside the `env_bootstrap` container.
- **FR-005**: Coordinare MUST mount the symphony's env cache volume read-only at `{container_devenv_root}/{sanitised_name}/` inside all other containerized performers dispatched for that symphony, provided the cache directory exists on the host.
- **FR-006**: The symphony env cache directory MUST be at `{env_cache_root}/{sanitised_symphony_name}/` on the host. Coordinare MUST create this directory before the first mount.
- **FR-007**: `env_cache_root` MUST be a new global configuration field defaulting to `~/.coordinare/env-caches/` (expanded to absolute path at load time).
- **FR-008**: If no `env_bootstrap` performer is configured for a symphony, FR-001 through FR-006 MUST be skipped silently for that symphony.
- **FR-009**: At most one `env_bootstrap` dispatch per symphony MUST be in flight at a time; subsequent change detections while a bootstrap is running MUST be queued and dispatched after the current one completes.
- **FR-010**: GitHub blob SHA fetch failures MUST be logged as warnings and MUST NOT overwrite the last known SHA or trigger a bootstrap.
- **FR-011**: The `env_bootstrap` performer role MUST appear in the dashboard's active sessions list with the symphony name and role clearly labelled.
- **FR-012**: Symphony names used as directory components MUST be sanitised to `[a-z0-9_-]` (lowercase, alphanumeric, dash, underscore); sanitisation MUST be deterministic and collision-resistant (append short hash on collision).
- **FR-013**: When dispatching any job to a containerized performer for a symphony with a ready env cache, coordinare MUST include `metadata.env_cache_path` in the `JobInitPayload`, set to the full in-container path `{container_devenv_root}/{sanitised_name}`. If no env cache is configured or the cache dir is not yet ready, the key MUST be absent from metadata.
- **FR-014**: For persistent performer containers, coordinare MUST mount all ready symphony env caches (across all symphonies) as read-only subdirectories under `{container_devenv_root}/` at container start time. Caches that become ready after the container has started are NOT retroactively mounted; coordinare MUST log a warning when this occurs.
- **FR-015**: After a persistent performer completes a job and before coordinare dispatches the next job to it, coordinare MUST call `POST /reset` on the performer. A 404 response MUST be treated as "not implemented" and logged as a warning without aborting the dispatch. Any other non-2xx response MUST also be logged as a warning without aborting.
- **FR-016**: `container_devenv_root` MUST be a new per-performer-endpoint config field (default `/devenv`) controlling the in-container path prefix for env cache subdirectories.

### Key Entities

- **EnvCacheState**: Per-symphony tracking of README SHA, bootstrap in-flight status, and last bootstrap time.
- **BootstrapJob**: The dispatch payload sent to the `env_bootstrap` performer, containing README content and symphony metadata.
- **SymphonyEnvCacheConfig**: New per-symphony config fields — `env_bootstrap_performer_id` (optional) and inherited `env_cache_root`.
- **VolumeMount** (existing): Extended to support dynamic injection from coordinare at dispatch time, not only from static per-performer config.
- **ResetEndpoint**: New `POST /reset` endpoint in the performer HTTP protocol, called by coordinare on persistent performers between jobs.

---

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Within one poll cycle of a README SHA change, an `env_bootstrap` dispatch is initiated for the affected symphony.
- **SC-002**: The env cache directory `{env_cache_root}/{symphony_name}/` exists on the host after the first successful bootstrap.
- **SC-003**: A containerized performer dispatched for a symphony with an existing env cache mounts the env subdirectory read-only and receives `metadata.env_cache_path` in the job init payload without any additional operator action.
- **SC-004**: Two symphonies with distinct README files result in two separate cache directories, each mounted only to performers of the correct symphony.
- **SC-005**: Coordinare correctly skips env-caching for a symphony with no `env_bootstrap` performer configured — no errors, no spurious volume mounts.
- **SC-006**: Bootstrap status is visible in the coordinare dashboard during an active bootstrap run.
- **SC-007**: No more than one `env_bootstrap` is dispatched concurrently per symphony.
- **SC-008**: The blob SHA GraphQL query for each symphony completes within the same latency budget as an existing `GET_FILE_CONTENT_QUERY` call (one round-trip per symphony per poll cycle; no additional API calls on a cache hit).
- **SC-009**: A persistent performer container started with two ready symphony env caches has both subdirectories mounted and correctly receives the appropriate `env_cache_path` for each job it handles.
- **SC-010**: `POST /reset` is called on a persistent performer between consecutive jobs; a performer that returns 404 continues to receive jobs without error.

---

## Scope

### In Scope

- Git blob SHA polling per symphony per cycle (README.md or configurable `env_spec_file`)
- `env_readme_sha` tracking in `CoordinareState`
- `env_cache_root` global config field
- `env_bootstrap_performer_id` per-symphony config field
- `container_devenv_root` per-performer-endpoint config field (default `/devenv`)
- `EnvCacheState` model (in-memory, per-symphony)
- Bootstrap dispatch logic and in-flight guard
- Dynamic `VolumeMount` injection at performer dispatch time (rw for bootstrap, ro for others)
- Subdirectory-per-symphony mount layout (`{container_devenv_root}/{sanitised_name}/`)
- `metadata.env_cache_path` in `JobInitPayload` pointing to the activated symphony's subdirectory
- Multi-symphony volume mounting for persistent performers at container start
- `POST /reset` call on persistent performers between jobs
- Host directory creation before first mount
- Symphony name sanitisation for filesystem paths
- Dashboard visibility of bootstrap sessions

### Out of Scope

- The performer-side logic of how the `env_bootstrap` image installs tools or sources the activation script (that is the performer container's responsibility)
- Cache invalidation strategies beyond README change detection (e.g., time-based expiry)
- Remote volume backends (NFS, S3, etc.) — local host path only
- Windows host support (Linux/macOS host paths only)
- Concurrent bootstrap runs (serialised per symphony by design)
- Automated cleanup of stale cache directories when a symphony is removed (future work)
- Dynamic remounting of new env caches into already-running persistent performer containers (requires restart)
- Resource limits (CPU/RAM) for bootstrap or regular performer containers (separate spec)

## Assumptions

- The `env_bootstrap` performer image is operator-provided and knows how to interpret README content, populate the cache subdirectory, and expose an activation script.
- The coordinare host filesystem has sufficient space for env caches.
- `env_bootstrap` performers always run as ephemeral containers (they are created fresh per bootstrap run).
- `CoordinareState` is reset on coordinare restart; on restart, coordinare re-fetches README SHAs and only skips bootstrap if the SHA is unchanged (meaning it fetches once at startup to seed the comparison baseline).
- The `VolumeMount` dynamic injection at dispatch time extends the existing `start_ephemeral` path; subprocess performers do not receive a volume mount.
- The performer image's activation contract (what it does with `env_cache_path`) is outside coordinare's scope; coordinare only guarantees delivery of the correct path.
