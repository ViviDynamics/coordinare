# Containerized Performers — Operator How-To

Containerized performers isolate work execution from the coordinare host, making them suitable for multi-machine deployments, audit isolation, and tool customization. This guide covers configuration, runtime, and troubleshooting.

## Overview

A performer is a unit of work execution registered with the coordinare. Traditionally, performers are native subprocesses on the coordinare host. With containerization, you can now run performers as:

- **Ephemeral containers**: Started on each job, torn down afterward. Useful for one-shot work or isolated tooling.
- **Persistent containers**: Long-running, reused across jobs. Efficient for multi-job workflows; requires idle-state management.
- **Subprocess (default)**: Native execution on the coordinare host; no changes required for existing deployments.

## Prerequisites

- Docker Engine ≥ 24.x reachable on the coordinare host (or via network socket).
- A performer image matching the documented contract (entrypoint, `/status` route, HTTP job protocol, authentication).
- Coordinare running on a branch with containerization support (v056 or later).

## Quick Start

### 1. Build or obtain a performer image

**Use a published variant** (recommended for most setups):

```bash
# Single backend (smallest image, fastest startup)
docker build -f agent/performer/Dockerfile.slim \
  --build-arg BACKEND=claude_code \
  -t performer:slim-claude .

# All backends + QA tooling (comprehensive, larger)
docker build -f agent/performer/Dockerfile.full \
  -t performer:full .

# Toolchain only (BYO CLI)
docker build -f agent/performer/Dockerfile.base \
  -t performer:base .
```

**Use a custom image** (advanced):
- Your image must expose `/status` (GET) and `/jobs` (POST/GET/stream/cancel) endpoints over HTTP.
- See [BYO Contract](#byo-contract) below for the full requirements.

### 2. Verify the image

Test the image locally before registering:

```bash
# Start the container
docker run --rm -p 8088:8088 \
  -e PERFORMER_AUTH_TOKEN=dev-token \
  performer:slim-claude

# In another terminal, check status
curl -s -H "Authorization: Bearer dev-token" \
  http://localhost:8088/status | jq .

# Expect output like:
# {
#   "availability": "idle",
#   "capabilities": {
#     "backends": ["claude_code"],
#     "tool_flags": ["git", "node", "python", "lint", "format", "test_runner", "ripgrep", "jq", "shell"]
#   },
#   "auth_enabled": true
# }
```

Stop the container: `docker stop <container_id>` (in the first terminal) or press Ctrl+C.

### 3. Register with the coordinare

Add an entry to `config.yaml`:

#### Persistent container (recommended)

Run the container separately; coordinare will dispatch to it:

```yaml
performer_endpoints:
  - id: claude-writer
    mode: persistent
    image: performer:slim-claude
    endpoint: http://localhost:8088
    roles: [writer, documenter]
    auth_token: ${PERFORMER_AUTH_TOKEN}
    failure_threshold: 5
    readiness_timeout_s: 120
    secret_sources:
      init_payload: true
      env: true
      creds_file: false
    volumes:
      - host_path: /path/to/project/.tools
        container_path: /opt/tools
        mode: ro
```

Start the container in the background:

```bash
docker run -d --name coordinare-performer-1 \
  -p 8088:8088 \
  -e PERFORMER_AUTH_TOKEN=<your-secret-token> \
  -v /path/to/project/.tools:/opt/tools:ro \
  performer:slim-claude
```

#### Ephemeral container (one-shot)

Coordinare starts the container per job and tears it down:

```yaml
performer_endpoints:
  - id: codex-ephemeral
    mode: ephemeral
    image: performer:slim-codex
    port: 8089
    roles: [writer]
    auth_token: ${PERFORMER_AUTH_TOKEN}
    failure_threshold: 5
    readiness_timeout_s: 60
```

Coordinare automatically manages the container lifecycle.

### 4. Start the coordinare

```bash
set -a && source .env && set +a
.venv/bin/python -m coordinare
```

Watch the dashboard (typically `http://localhost:8000`). The performer pool widget shows:

- Registration id
- Mode (subprocess/ephemeral/persistent)
- Availability (idle/busy/starting/unreachable/draining)
- Current job id (if busy)
- Last successful status check
- Consecutive failures (if excluded)
- Auth enabled status

### 5. Dispatch a card

Drop a card matching one of the performer's roles. The coordinare will:

1. Poll the performer's `/status` endpoint.
2. Check capabilities against the card's requirements.
3. Dispatch the job via POST `/jobs`.
4. Poll or stream job progress.
5. (For ephemeral only) Tear down the container on completion.

## Configuration Reference

### Performer registration fields

```yaml
performer_endpoints:
  - id: string                          # Unique identifier
    mode: subprocess|ephemeral|persistent  # Execution mode (default: subprocess)
    image: string                       # Docker image tag (required for ephemeral/persistent)
    endpoint: url                       # HTTP endpoint (required for persistent)
    port: int                           # Ephemeral port (optional; default: auto-assigned)
    roles: [string, ...]                # Role labels this performer serves
    auth_token: string                  # Bearer token (optional; disable auth if omitted)
    failure_threshold: int              # Consecutive failures before exclusion (default: 5)
    readiness_timeout_s: int            # Max wait for startup (default: 120)
    capability_overrides:               # (Advanced) explicit capabilities if image reports incorrect ones
      backends: [string, ...]
      tool_flags: [string, ...]
    secret_sources:
      init_payload: bool                # Allow secrets in job-init payload (default: true)
      env: bool                         # Allow secrets from container env vars (default: true)
      creds_file: bool                  # Allow secrets from mounted creds file (default: false)
    volumes:
      - host_path: string               # Host path to mount
        container_path: string          # Container path
        mode: ro|rw                     # Read-only or read-write (default: ro)
```

### Secret precedence

When a job requires a secret, the performer checks sources in this order:

1. **Job-init payload** (if `secret_sources.init_payload` is true)
2. **Environment variables** (if `secret_sources.env` is true)
3. **Mounted creds file** (if `secret_sources.creds_file` is true)

The first non-empty value is used. If no source provides the secret, the job fails with a `secret_missing` error visible in the coordinare logs and dashboard.

### Volume mounts

Mount project-specific tools or files into ephemeral containers:

```yaml
volumes:
  - host_path: /usr/local/bin/my-cli
    container_path: /opt/my-cli/bin/my-cli
    mode: ro
  - host_path: /home/user/.config/myapp
    container_path: /root/.config/myapp
    mode: ro
```

For persistent containers, you manage mounts yourself via `docker run -v`.

## Troubleshooting

### Container won't start

Check Docker logs:

```bash
docker logs <container_id>
```

Common issues:
- Image not found: `docker pull` or rebuild.
- Port already in use: choose a different port or kill the conflicting process.
- Missing env vars: ensure `PERFORMER_AUTH_TOKEN` (if configured) is set.

### Performer marked unreachable

The coordinare failed `failure_threshold` consecutive `/status` checks. Check:

1. Container is running: `docker ps | grep performer`
2. Network reachability: `curl -v http://localhost:8088/status`
3. Auth token matches: `echo $PERFORMER_AUTH_TOKEN` and compare to registration config.
4. Logs: `docker logs <container_id>` (look for 401, 403, or connection errors).

Recovery is automatic once `/status` succeeds again.

### Card fails with "capability mismatch"

The performer image lacks a tool or backend the card's role requires. Options:

1. Use a different performer image with the needed capability.
2. Mount the tool via `volumes` (if it's not built into the image).
3. Override capabilities in config (advanced; use only if the image *has* the tool but reports it incorrectly):

   ```yaml
   capability_overrides:
     backends: [claude_code]
     tool_flags: [git, node, python, browser, lint, format, test_runner, ripgrep, jq, shell]
   ```

### Secrets not resolving

Check the `secret_sources` config and ensure at least one source is enabled and populated. Logs show which source was used (e.g., `secret_resolved source=init_payload`). Missing secrets log a clear error like `secret_missing name=GITHUB_TOKEN`.

## Reconfiguring a registration (drain-then-reapply)

If you need to change `mode`, `image`, or `endpoint` while a job is in flight:

1. **Mark as draining** (optional, but recommended for persistent):
   ```bash
   # The performer stops accepting new jobs but finishes the current one.
   # This is automatic if you just stop accepting submissions.
   ```

2. **Wait for the current job to finish**:
   - Watch the dashboard pool widget for `current_job_id` to clear.
   - Monitor logs for the job reaching a terminal state.

3. **Edit config** and reload:
   ```bash
   # Edit config.yaml, change the performer entry, then reload:
   # curl -X POST http://localhost:8000/admin/reload-config
   # (or use your config-reload endpoint)
   ```

4. **Perform readiness check**:
   - The coordinare polls `/status` against the new endpoint/mode.
   - On success, availability returns to `idle`.
   - On failure, the performer is marked `unreachable` pending recovery.

Forcing a config change while `current_job_id` is set will be rejected to prevent job loss.

## Advanced: BYO Contract

To bring your own performer image, implement these endpoints over HTTP:

### `GET /status`

Returns the performer's current state.

**Response** (200 OK):

```json
{
  "availability": "idle",
  "current_job_id": null,
  "capabilities": {
    "backends": ["claude_code"],
    "tool_flags": ["git", "node", "python", "lint", "format", "test_runner", "ripgrep", "jq", "shell", "browser"]
  },
  "auth_enabled": true
}
```

**Fields**:
- `availability`: one of `idle`, `busy`, `starting`, `draining` (string).
- `current_job_id`: null or a string UUID.
- `capabilities.backends`: list of backend identifiers the image supports (e.g., `["claude_code"]`).
- `capabilities.tool_flags`: list of tool capability flags (extensible; performers must ignore unknown flags).
- `auth_enabled`: boolean indicating whether the performer requires bearer-token auth.

### `POST /jobs`

Submit a new job.

**Request body**:

```json
{
  "job_id": "uuid",
  "card_id": "uuid",
  "role": "implementer",
  "backend": "claude_code",
  "persona": "You are a careful implementer...",
  "repo_url": "https://github.com/org/repo",
  "branch": "feature/my-branch",
  "secrets": {"GITHUB_TOKEN": "***"},
  "metadata": {}
}
```

**Response** (202 Accepted):

```json
{
  "job_id": "uuid",
  "state": "accepted",
  "message": "Job queued"
}
```

**Response** (409 Conflict — performer busy):

```json
{
  "reason": "busy",
  "detail": "Performer is executing job <other_id>"
}
```

**Response** (422 Unprocessable Entity — missing secret):

```json
{
  "reason": "secret_missing",
  "detail": "GITHUB_TOKEN"
}
```

### `GET /jobs/{job_id}`

Poll job status.

**Response** (200 OK):

```json
{
  "job_id": "uuid",
  "state": "running",
  "message": "Executing card..."
}
```

Terminal states: `succeeded`, `failed`, `cancelled`. Include a `result` field with job output for terminal states.

### `GET /jobs/{job_id}/stream`

Stream job status over SSE.

**Response** (200 OK):

Server-sent events, one per line:

```
data: {"job_id":"uuid","state":"accepted"}
data: {"job_id":"uuid","state":"running"}
data: {"job_id":"uuid","state":"succeeded","result":{...}}
```

Closes connection on terminal state.

### `POST /jobs/{job_id}/cancel`

Cancel an in-flight job.

**Response** (200 OK):

```json
{
  "job_id": "uuid",
  "state": "cancelled"
}
```

**Response** (404 Not Found):

```json
{
  "error": "job not found"
}
```

## Performance Targets

- `/status` poll latency: p95 < 250ms
- `/jobs` dispatch ack: p95 < 500ms
- Ephemeral cold-start: ≤ 60s (slim), ≤ 120s (full)

Monitor via dashboard or Prometheus (metrics: `performer_pool_status_polls_total`, `performer_pool_dispatch_total`).

## Getting Help

- **Dashboard**: Visit the performer pool widget to see real-time state and exclusion reasons.
- **Logs**: Check coordinare and performer logs for structured error messages; no secret values are logged.
- **Notifications**: Exclusion and recovery events are emitted via Slack, email, or dashboard depending on your notification config.
