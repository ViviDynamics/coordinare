# performer

Docker-containerised agent that implements the coordinare wire protocol
(JSON on stdin → JSON on stdout).  The performer clones a repository,
runs an AI coding backend, then opens a GitHub pull request when the
backend finishes.

---

## 1. Build the base image

The **base** image ships Python 3.12, git, the opencode CLI, and the
performer package.  It is the minimal image for agentic coding tasks.

```sh
docker build -t coordinare-performer:base agent/performer/
```

Run from the repository root.

---

## 2. Build the full image

The **full** image extends the base with Node.js LTS and build tools,
supporting front-end and full-stack coding tasks.

```sh
docker build \
  -t coordinare-performer:full \
  -f agent/performer/images/full/Dockerfile \
  agent/performer/
```

Requires the base image to be built first.

---

## 3. Extend the base image for a custom stack

Create a `Dockerfile` in your project that inherits from the base:

```dockerfile
FROM coordinare-performer:base

# Example: add Rust toolchain
RUN curl https://sh.rustup.rs -sSf | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"
```

Do **not** override `ENTRYPOINT` — it must remain `["python", "-m", "performer"]`.

---

## 4. Run a local test performance

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

## 5. Verify protocol compliance

Run the protocol contract test to confirm that `PerformerResponse` is a
superset of the coordinare's `ProtocolResponse`:

```sh
cd agent/performer
pip install -e ".[dev]"
pytest tests/unit/test_protocol_contract.py -v
```

---

## 6. Environment variables reference

| Variable | Default | Description |
|---|---|---|
| `AGENT_BACKEND` | `opencode` | Coding backend to use. Supported: `opencode`, `junie`, `cursor`, `claude_code`, `codex`. |
| `AGENT_TIMEOUT` | `1800` | Maximum seconds for a single performance before the backend is killed and the session transitions to `error`. |

---

## Wire protocol

All messages are newline-delimited JSON.

**Inbound** (`PerformerMessage`):

```json
{"action": "dispatch|status|relay_feedback|health", "session_id": "", "payload": {}}
```

**Outbound** (`PerformerResponse`):

```json
{"status": "accepted", "session_id": "550e8400-e29b-41d4-a716-446655440000"}
```

Null/empty fields are omitted (`exclude_none=True`). The full set of possible
`status` values is:
`accepted`, `working`, `pr_opened`, `blocked`, `error`, `unknown`, `busy`,
`acknowledged`, `session_expired`, `healthy`, `unhealthy`.

Optional fields present only when relevant: `pr_url`, `pr_node_id`, `reason`,
`questions`, `progress`, `metrics`.

The performer exits after emitting a `pr_opened` or `error` status response.
