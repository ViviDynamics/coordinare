# Phase 1 Data Model — Performer Environment Caching (060)

In-memory only (except the host-filesystem cache directory). No new persistence backend.
New Pydantic models live in `src/coordinare/models/env_cache.py`.
Config additions extend `src/coordinare/config.py`.
`CoordinareState` gains one new key.

---

## 1. Config Changes (config.py)

### GlobalConfig — new field

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `env_cache_root` | `Path \| None` | no | `~/.coordinare/env-caches/` | Absolute path after `__post_init__` expansion. Parent directories are created by coordinare at startup. |

### SymphonyConfig — new field

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `env_bootstrap_performer_id` | `str \| None` | no | `None` | References a performer registration ID in `orchestra.performers`. When `None`, env-caching is disabled for this symphony. |
| `env_spec_files` | `list[str]` | no | `["README.md"]` | Relative paths of files whose content is monitored. Any change to any watched file triggers a bootstrap. Editable from the dashboard without restart. |

### Validation rules

- If `env_bootstrap_performer_id` is set, a performer registration with that ID MUST exist in `orchestra.performers`; otherwise a validation error is raised at config-load.
- Each entry in `env_spec_files` MUST be a relative POSIX path (no leading `/`).
- `env_cache_root` path MUST NOT be inside a container mount path.

---

## 2. EnvCacheState (in-memory, per-symphony)

Lives on `CoordinareState.env_cache: dict[str, EnvCacheState]` keyed by symphony `name`.

```python
class EnvCacheState(BaseModel):
    symphony_name: str
    sanitised_name: str          # filesystem-safe slug, derived once at startup
    cache_dir: Path              # absolute path: {env_cache_root}/{sanitised_name}/
    readme_sha: str | None = None        # last known blob SHA; None = never fetched
    bootstrap_in_flight: bool = False    # True while a bootstrap dispatch is active
    cache_dir_ready: bool = False        # True once the cache dir has been created on disk
    pending_sha: str | None = None       # SHA queued while bootstrap_in_flight; None = no pending
    last_bootstrap_at: datetime | None = None
    last_bootstrap_succeeded: bool | None = None  # None = never run
```

State transitions:

```
                      ┌──────────────────────────────────┐
                      │  bootstrap_in_flight = False      │
                      │  readme_sha = None or stale       │
                      └──────────────────────────────────┘
                                     │ SHA change detected
                                     ▼
                      ┌──────────────────────────────────┐
                      │  bootstrap_in_flight = True       │
                      │  (dispatch sent)                  │
                      └──────────────────────────────────┘
                        │ success                  │ failure
                        ▼                          ▼
          ┌──────────────────────┐    ┌──────────────────────────┐
          │ bootstrap_in_flight  │    │ bootstrap_in_flight=False │
          │   = False            │    │ last_bootstrap_succeeded  │
          │ readme_sha = new     │    │   = False                 │
          │ last_bootstrap_at = now   │ sha unchanged (retry next │
          │ last_bootstrap_succ       │   cycle if SHA still old) │
          │   = True             │    └──────────────────────────┘
          └──────────────────────┘
```

---

## 3. BootstrapJobPayload

Sent to the `env_bootstrap` performer via the existing job dispatch protocol (same format as regular card jobs, with a distinct job type).

```python
class BootstrapJobPayload(BaseModel):
    job_type: Literal["env_bootstrap"] = "env_bootstrap"
    symphony_name: str
    symphony_org: str
    symphony_repo: str
    env_spec_files: list[str]          # e.g. ["README.md"]
    env_spec_contents: dict[str, str]  # file path → full text content
    cache_mount_path: str = "/devenv/symphony"  # full in-container subdirectory path, e.g. "/devenv/my-project-a1b2c3"
```

**Note**: `env_spec_contents` are fetched only when a combined SHA change is detected (reusing `github.get_file_content`), avoiding a content download on every cycle.

---

## 4. Dynamic VolumeMount Injection

`get_env_volume_for_symphony` is a pure function called from the performer dispatch path. It returns both the `VolumeMount` and the resolved in-container path (used to populate `metadata.env_cache_path` in `JobInitPayload`).

```python
def get_env_volume_for_symphony(
    symphony_name: str,
    env_cache_states: dict[str, EnvCacheState],
    *,
    is_bootstrap: bool,
    container_devenv_root: str = "/devenv",
) -> tuple[VolumeMount, str] | None:
    state = env_cache_states.get(symphony_name)
    if state is None or not state.cache_dir_ready:
        return None
    container_path = f"{container_devenv_root}/{state.sanitised_name}"
    mode: Literal["rw", "ro"] = "rw" if is_bootstrap else "ro"
    return VolumeMount(
        host_path=state.cache_dir,
        container_path=container_path,
        mode=mode,
    ), container_path
```

The result is consumed in the dispatch layer: the `VolumeMount` is appended to the performer's `volumes` list before `start_ephemeral` is invoked, and `container_path` is set as `metadata["env_cache_path"]` in the `JobInitPayload`. The `VolumeMount` model and `start_ephemeral` wiring are otherwise unchanged.

For **persistent performers**, `_collect_env_volumes_for_persistent_performer()` is called instead (see §5) to mount all ready symphony caches at once. This collection happens in the daemon/dispatch layer — not inside `start_ephemeral()`.

---

## 5. EnvCacheService

New service class in `src/coordinare/services/env_cache.py`. Holds all env-caching logic.

```python
class EnvCacheService:
    def __init__(
        self,
        github: GitHubClient,
        env_cache_root: Path,
        symphonies: list[SymphonyConfig],
        orchestra: OrchestraConfig,
    ) -> None: ...

    async def initialise(self, state: dict[str, EnvCacheState]) -> None:
        """Seed state on startup: create cache dirs, fetch initial SHAs."""

    async def check_and_trigger(
        self,
        symphony: SymphonyConfig,
        state: dict[str, EnvCacheState],
        dispatch_fn: Callable[[str, BootstrapJobPayload], Awaitable[None]],
    ) -> None:
        """Called once per symphony per poll cycle. Fetches SHA, triggers bootstrap if changed."""

    def on_bootstrap_complete(
        self, symphony_name: str, success: bool, state: dict[str, EnvCacheState]
    ) -> None:
        """Update in-flight flag and record outcome."""
```

---

## 6. CoordinareState Extension

```python
# New key in CoordinareState TypedDict (state_store.py or graph/state.py)
env_cache: dict[str, EnvCacheState]   # keyed by symphony name; empty dict by default
```

---

## 7. Symphony Name Sanitisation

```python
def sanitise_symphony_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]", "-", name.lower()).strip("-")
    if not slug:
        slug = "symphony"
    suffix = hashlib.sha1(name.encode()).hexdigest()[:6]
    return f"{slug}-{suffix}"
```

The 6-char SHA1 suffix is **always** appended (not only on collision). This guarantees global uniqueness: two names that produce the same slug base (e.g. `"Foo Bar"` → `foo-bar` and `"foo-bar"` → `foo-bar`) still get distinct suffixes because their SHA1s differ. The function is deterministic — same input always produces the same output.

---

## 8. Relationships

```
GlobalConfig.env_cache_root
         │
         ▼
EnvCacheState.cache_dir = {env_cache_root}/{sanitised_symphony_name}/
         │
         ├──(rw)──▶ BootstrapJobPayload → env_bootstrap PerformerEndpointConfig
         │
         └──(ro)──▶ VolumeMount injected into every other containerised performer
                     dispatched for that symphony
```
