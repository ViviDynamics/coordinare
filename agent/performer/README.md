# performer

Docker-containerised agent that runs CI jobs submitted by the coordinare.
The performer receives a job payload over HTTP, clones a repository,
runs an AI coding backend, and reports results back via HTTP.

---

## 1. Build the base image

The **base** image ships Python 3.12, git, Node.js 20, jq, ripgrep, bash,
and the performer HTTP server.  No backend CLIs or browser.

```sh
docker build -t coordinare-performer:base -f Dockerfile.base agent/performer/
```

Run from the repository root.

---

## 2. Build a slim image for a specific backend

The **slim** image extends the base with a single backend CLI and optional
Playwright for browser automation.  Choose one of: `claude_code`, `codex`,
`junie`, `opencode`.

```sh
docker build \
  -t coordinare-performer:slim-claude-code \
  -f Dockerfile.slim \
  --build-arg BACKEND=claude_code \
  agent/performer/
```

Add `--build-arg BROWSER=true` to include Playwright + Chromium:

```sh
docker build \
  -t coordinare-performer:slim-claude-code-browser \
  -f Dockerfile.slim \
  --build-arg BACKEND=claude_code \
  --build-arg BROWSER=true \
  agent/performer/
```

---

## 3. Build the full image

The **full** image extends the base with all five backends, Playwright,
and cross-language linters, formatters, and test runners.

```sh
docker build \
  -t coordinare-performer:full \
  -f Dockerfile.full \
  agent/performer/
```

Run from the repository root.

---

## 4. Bring Your Own (BYO) image

Any Docker image can be registered with the coordinare as long as it
satisfies the **HTTP Job Protocol Contract** (see §5 below).

Create a `Dockerfile` that extends the base or starts from scratch:

```dockerfile
FROM coordinare-performer:base

# Example: add Rust toolchain
RUN curl https://sh.rustup.rs -sSf | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"
```

The entrypoint **MUST** serve HTTP on the port specified by the `PORT`
environment variable (default `8088`). See §5 for the required routes and
response schemas.

---

## 5. HTTP Job Protocol Contract

Every performer image (bundled or BYO) must satisfy this contract to be
registered with the coordinare.

### Required Routes

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/status` | Poll performer availability and capabilities |
| `POST` | `/jobs` | Submit a new job for execution |
| `GET` | `/jobs/{jobId}` | Poll job status and result |
| `GET` | `/jobs/{jobId}/stream` | Stream job progress (Server-Sent Events) |
| `POST` | `/jobs/{jobId}/cancel` | Request job cancellation |

### Authentication

When a per-performer bearer token is configured in the coordinare registration,
every request **MUST** include:

```
Authorization: Bearer <token>
```

The performer rejects missing or mismatched tokens with HTTP 401. When no
token is configured (operator opt-out, development only), endpoints accept
anonymous requests.

### `/status` Response Schema

```json
{
  "availability": "idle|starting|busy|draining",
  "capabilities": {
    "backends": ["claude_code", "opencode"],
    "tool_flags": ["git", "python", "browser", "lint"]
  },
  "auth_enabled": true,
  "current_job_id": null,
  "version": "1.0.0"
}
```

**Required fields**: `availability`, `capabilities` (with `backends` and
`tool_flags` lists), `auth_enabled`.

**Tool Flag Enumeration (v1)**:
- `git`: Git version control
- `node`: Node.js runtime
- `python`: Python runtime
- `browser`: Playwright / headless browser
- `lint`: Code linting tools
- `format`: Code formatters
- `test_runner`: Test execution frameworks
- `ripgrep`: Fast regex search
- `jq`: JSON query tool
- `shell`: Shell scripting

Performers MUST advertise the tool flags that are installed and available.
Unknown flags on inbound requests are ignored.

### `/jobs` Request and Response

**Request** (POST body):

```json
{
  "job_id": "jid-123",
  "card_id": "card-456",
  "role": "feature-dev",
  "backend": "claude_code",
  "persona": "expert-python-dev",
  "repo_url": "https://github.com/org/repo",
  "branch": "feat/new-feature",
  "secrets": {
    "GITHUB_TOKEN": "ghp_..."
  },
  "metadata": {}
}
```

**Success Response** (HTTP 202):

```json
{
  "accepted": true,
  "job_id": "jid-123",
  "started_at": "2026-01-15T10:30:00Z"
}
```

**Busy Response** (HTTP 409):

```json
{
  "accepted": false,
  "reason": "busy|draining|auth_failed|capability_mismatch|secret_missing",
  "retry_after_s": 10,
  "detail": "Performer is currently processing another job"
}
```

Secret values **MUST NEVER** appear in any HTTP response, error message, or log.

### Full Contract Reference

See `specs/056-performer-containerization/contracts/performer-http.openapi.yaml`
for the complete OpenAPI 3.0 specification.

---

## 6. Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8088` | HTTP server listen port. Overrides the CLI `--port` flag. |

### v1 Performer Entrypoint (deprecated stdin/stdout protocol)

See earlier versions of this README for the JSON-over-stdin/stdout protocol.

---

## 7. Run a local test job

Start a container with stdin/stdout attached:

```sh
docker run --rm -i \
  -e AGENT_BACKEND=opencode \
  coordinare-performer:base
```

Send a **dispatch** message:

```json
{"action": "dispatch", "session_id": "", "payload": {"title": "Add README badge", "description": "Add a CI badge to README.md", "acceptance_criteria": ["Badge is visible in README"], "repo_url": "https://github.com/org/repo", "branch": "feat/badge", "github_token": "ghp_YourTokenHere"}}
```

Poll with a **status** message (replace `<session_id>` with the value from the accepted response):

```json
{"action": "status", "session_id": "<session_id>", "payload": {}}
```

Relay feedback when the backend is **blocked**:

```json
{"action": "relay_feedback", "session_id": "<session_id>", "payload": {"feedback": "Use GitHub Actions for CI"}}
```

Check container readiness with a **health** message:

```json
{"action": "health", "session_id": "", "payload": {}}
```

---

## 8. Test a job submission locally

Start a performer container with the HTTP server exposed:

```sh
docker run -p 8088:8088 coordinare-performer:base
```

In another terminal, submit a job:

```sh
curl -X POST http://localhost:8088/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "job_id": "job-123",
    "card_id": "card-abc",
    "role": "feature-dev",
    "backend": "opencode",
    "persona": "expert-python",
    "repo_url": "https://github.com/org/repo",
    "branch": "feature-branch",
    "secrets": {},
    "metadata": {}
  }'
```

Poll the job:

```sh
curl http://localhost:8088/jobs/job-123
```

Stream progress:

```sh
curl http://localhost:8088/jobs/job-123/stream
```

---

## 9. Verify test coverage

Run the HTTP contract tests:

```sh
cd agent/performer
pip install -e ".[dev]"
pytest tests/contract/ -v
```

---

## Deprecated: Stdin/stdout protocol (v0)

Earlier versions used a JSON-over-stdin/stdout protocol. This is no longer
supported. All new implementations must use the HTTP Job Protocol.
