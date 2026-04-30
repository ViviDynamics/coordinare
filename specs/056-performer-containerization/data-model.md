# Phase 1 Data Model — Containerized Performer Execution

In-memory only. No new persistence backend. Pydantic v2 models live under `src/coordinare/models/performer_endpoint.py`; configuration models extend the existing performer config schema.

## 1. PerformerEndpointConfig (config-side)

Operator-supplied configuration for one performer registration. Loaded from `config.yaml` (extended `performers:` block).

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `id` | `str` | yes | — | Unique within deployment. Used as the registration key. |
| `mode` | `Literal["subprocess", "ephemeral", "persistent"]` | yes | `subprocess` | Existing subprocess path is the default to satisfy FR-002. |
| `roles` | `list[str]` | yes | — | Roles this performer is eligible for. Cross-checked against `PerformerCapabilities`. |
| `image` | `str \| None` | when mode != subprocess | `None` | Image reference, e.g. `performer:full` or `myorg/perf:custom`. |
| `endpoint` | `HttpUrl \| None` | when mode == persistent | `None` | Pre-known address for long-running container. |
| `port` | `int \| None` | when mode == ephemeral | `None` | Host port to bind; ephemeral lifecycle picks one if unset. |
| `auth_token` | `SecretStr \| None` | no | `None` | Optional bearer token. `None` ⇒ auth disabled (dev opt-out, FR-023a). |
| `readiness_timeout_s` | `int` | no | `120` | Per FR-004a default. |
| `failure_threshold` | `int` | no | `5` | Consecutive failed status checks before exclusion (FR-012). |
| `secret_sources` | `SecretSourceConfig` | no | all-enabled | Per-performer enable/disable for the three sources (FR-021). |
| `volumes` | `list[VolumeMount]` | no | `[]` | Project-specific tool mounts (FR-018). |
| `capability_overrides` | `CapabilityOverride \| None` | no | `None` | Allow operator to declare BYO-image capabilities up front (FR-025). |

### SecretSourceConfig

```python
class SecretSourceConfig(BaseModel):
    init_payload: bool = True   # over-the-wire on job init
    env: bool = True            # container env vars
    creds_file: bool = True     # mounted creds dir/file
```

### VolumeMount

```python
class VolumeMount(BaseModel):
    host_path: Path
    container_path: PurePosixPath
    mode: Literal["ro", "rw"] = "ro"
```

### CapabilityOverride

```python
class CapabilityOverride(BaseModel):
    backends: list[str] = []
    tool_flags: list[Literal["git","node","python","browser","lint","format","test_runner","ripgrep","jq","shell"]] = []
```

## 2. PerformerCapabilities (runtime-advertised)

Returned by performer's `GET /status` and cached on the coordinare side per registration.

```python
class PerformerCapabilities(BaseModel):
    backends: list[str]
    tool_flags: list[str]   # validated against the v1 enumeration; unknowns ignored on read
```

Validation rule: when matching a role's requirement, the coordinare computes the required `(backend, tool_flags)` pair from persona definitions and the role catalog, then asserts every required item is present.

## 3. PerformerEndpointState (in-memory, coordinare-side)

Lives on `CoordinareState.performer_endpoints: dict[str, PerformerEndpointState]`. Resets on coordinare restart.

| Field | Type | Notes |
|---|---|---|
| `id` | `str` | Mirrors config. |
| `mode` | `Literal["ephemeral", "persistent"]` | Subprocess performers are not stored here. |
| `endpoint` | `HttpUrl \| None` | Resolved at registration (persistent) or container-start time (ephemeral). |
| `availability` | `Literal["unknown", "starting", "idle", "busy", "draining", "unreachable"]` | Drives dispatch eligibility. |
| `capabilities` | `PerformerCapabilities \| None` | Populated on first successful status check. |
| `current_job_id` | `str \| None` | Set when busy. |
| `last_status_at` | `datetime \| None` | Used for staleness detection. |
| `consecutive_failures` | `int` | Reset to 0 on every successful status check. |
| `excluded_until_recovery` | `bool` | Set when `consecutive_failures >= failure_threshold`. |

State transitions:

```
unknown → starting    (registered or container started)
starting → idle       (first successful status check)
starting → unreachable (readiness timeout exceeded)
idle → busy           (job dispatch acknowledged)
busy → idle           (terminal job state observed)
busy → unreachable    (consecutive failures >= threshold while busy)
idle → unreachable    (consecutive failures >= threshold while idle)
unreachable → idle    (next successful status check; emits recovery notification)
* → draining          (operator-initiated; coordinare stops dispatching)
```

Every transition emits a structured `performer_endpoint.transition` event (existing observability conventions).

## 4. Job (over-the-wire)

```python
class JobInitPayload(BaseModel):
    job_id: str                       # coordinare-generated, monotonic per session
    card_id: str
    role: str
    backend: str
    persona: str
    repo_url: HttpUrl
    branch: str
    secrets: dict[str, SecretStr] = {}   # init-payload secrets (FR-019/020)
    metadata: dict[str, Any] = {}        # forward-compat extension point
```

```python
class JobAcceptResponse(BaseModel):
    accepted: Literal[True] = True
    job_id: str
    started_at: datetime
```

```python
class JobBusyResponse(BaseModel):
    accepted: Literal[False] = False
    reason: Literal["busy", "draining", "auth_failed", "capability_mismatch", "secret_missing"]
    retry_after_s: int | None = None
    detail: str | None = None
```

```python
class JobStatus(BaseModel):
    job_id: str
    state: Literal["accepted", "running", "succeeded", "failed", "cancelled"]
    progress_pct: int | None = None
    last_event: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    result: JobResult | None = None     # populated when state in (succeeded, failed)
```

```python
class JobResult(BaseModel):
    success: bool
    summary: str
    diff_url: HttpUrl | None = None
    logs_excerpt: str | None = None
    error_code: str | None = None       # e.g. "secret_missing:GITHUB_TOKEN"
```

## 5. PerformerPool (service)

Not a stored entity; a service that owns the in-memory `registrations: dict[str, PerformerEndpointState]` (mirrored on `CoordinareState.performer_endpoints` per §3) and exposes:

- `register(config: PerformerEndpointConfig) -> None`
- `unregister(id: str) -> None`
- `poll_all() -> None` — fans out `GET /status` to every entry in `registrations`; updates state, transitions, fires notifications.
- `select_for(role: str, backend: str, required_flags: set[str]) -> PerformerEndpointState | None` — returns the first idle, capability-matching, non-excluded candidate; deterministic ordering by registration order.
- `mark_busy(id: str, job_id: str) -> None` / `mark_idle(id: str) -> None`
- `mark_unreachable(id: str) -> None` / `mark_recovered(id: str) -> None`

Concurrency: a single `asyncio.Lock` per entry in `registrations` guards busy/idle transitions to prevent the dispatch loop from racing the status poll. Selection is nonblocking and operates on the snapshot.

## 6. Validation / invariants

- `mode == "persistent"` ⇒ `endpoint` is set.
- `mode == "ephemeral"` ⇒ `image` is set.
- `mode == "subprocess"` ⇒ `image`, `endpoint`, `auth_token` are all unset (config rejected otherwise).
- `failure_threshold >= 1`.
- `readiness_timeout_s >= 1`.
- A registration whose `capability_overrides` contradict the live `GET /status` response transitions to `unreachable` with a `capability_mismatch` event (SC-006).
- The same `endpoint` MUST NOT appear under two registrations (edge case from spec.md — duplicate-address detection).
