# Quickstart: 012-performer

**Local testing guide for the performer container**

---

## Prerequisites

- Docker installed and running
- A GitHub personal access token (classic or fine-grained) with `repo` scope (read + write + pull request creation)
- A target GitHub repository you own (or have write access to) — used for the test dispatch
- `opencode` API key configured (e.g. `ANTHROPIC_API_KEY` for Claude-backed opencode)

---

## 1. Build the Base Image

From the repo root:

```bash
docker build -t coordinare-performer:base agent/performer/
```

To also build the full image (adds Node.js LTS, Python 3.12, POSIX build tools):

```bash
docker build -t coordinare-performer:full agent/performer/images/full/
```

---

## 2. Verify Protocol Compliance (Health Check)

Confirm the performer responds to the health action within 2 seconds:

```bash
echo '{"action": "health", "session_id": "", "payload": {}}' \
  | docker run --rm -i \
    -e AGENT_BACKEND=opencode \
    -e ANTHROPIC_API_KEY=your_key_here \
    coordinare-performer:base
```

Expected output:
```json
{"status": "healthy"}
```

If `AGENT_BACKEND` is set to an unsupported value, you should see:
```json
{"status": "unhealthy", "reason": "AGENT_BACKEND=foobar is not a supported backend"}
```

---

## 3. Run a Test Performance (End-to-End)

### 3a. Prepare the dispatch payload

Create a file `dispatch.json`:

```json
{
  "action": "dispatch",
  "session_id": "",
  "payload": {
    "title": "Add a README badge",
    "description": "Add a CI status badge to the top of the README.md file.",
    "acceptance_criteria": [
      "README.md contains a CI badge in the first 5 lines",
      "Badge uses the correct GitHub Actions workflow URL"
    ],
    "repo_url": "https://github.com/YOUR_ORG/YOUR_REPO",
    "branch": "test/performer-badge",
    "github_token": "ghp_your_token_here"
  }
}
```

### 3b. Dispatch

```bash
cat dispatch.json \
  | docker run --rm -i \
    -e AGENT_BACKEND=opencode \
    -e ANTHROPIC_API_KEY=your_key_here \
    -e AGENT_TIMEOUT=1800 \
    coordinare-performer:base
```

The performer responds immediately with:
```json
{"status": "accepted", "session_id": "550e8400-e29b-41d4-a716-446655440000"}
```

### 3c. Poll status

The performer is stateful within a single stdin/stdout process invocation. Send all
messages (dispatch, status, relay_feedback) into the **same** running container by
piping them together:

```bash
SESSION_ID="550e8400-e29b-41d4-a716-446655440000"

printf '%s\n%s\n' \
  '{"action":"dispatch","payload":{"title":"T","repo_url":"https://github.com/org/repo","branch":"feat/x","github_token":"ghp_..."}}' \
  "{\"action\":\"status\",\"session_id\":\"$SESSION_ID\",\"payload\":{}}" \
  | docker run --rm -i coordinare-performer:base
```

> **Note**: A new `docker run` cannot see a prior session — it will return
> `session_expired`. All messages must go through the same stdin stream.

Expected while in progress:
```json
{
  "status": "working",
  "session_id": "550e8400-...",
  "progress": "Modifying README.md",
  "metrics": {"pid": 1, "child_pids": [7, 8], "memory_bytes": 120000000, "cpu_percent": 12.3, "tokens_processed": null}
}
```

Expected on completion:
```json
{
  "status": "pr_opened",
  "session_id": "550e8400-...",
  "pr_url": "https://github.com/YOUR_ORG/YOUR_REPO/pull/5",
  "pr_node_id": "PR_kwDOABcD12345"
}
```

---

## 4. Test relay_feedback (Unblocking a Blocked Performance)

If the performer returns `blocked`:
```json
{
  "status": "blocked",
  "session_id": "550e8400-...",
  "questions": ["Should I use the GitHub Actions CI workflow or CircleCI for the badge URL?"]
}
```

Send feedback:
```bash
SESSION_ID="550e8400-e29b-41d4-a716-446655440000"

echo "{\"action\": \"relay_feedback\", \"session_id\": \"$SESSION_ID\", \"payload\": {\"feedback\": \"Use GitHub Actions CI workflow URL.\"}}" \
  | docker run --rm -i coordinare-performer:base
```

Expected response:
```json
{"status": "acknowledged", "session_id": "550e8400-..."}
```

Then resume polling status as in step 3c.

---

## 5. Test with the Full Image (Node.js / Python Projects)

```bash
cat dispatch.json \
  | docker run --rm -i \
    -e AGENT_BACKEND=opencode \
    -e ANTHROPIC_API_KEY=your_key_here \
    coordinare-performer:full
```

No additional setup needed. The full image provides Node.js LTS, Python 3.12, npm, pip, and common POSIX build tools.

---

## 6. Verify Image Tiers are Distinct

```bash
# Check base image has no node/npm
docker run --rm coordinare-performer:base which node 2>&1 || echo "node not present (expected)"

# Check full image has node/npm
docker run --rm coordinare-performer:full node --version
docker run --rm coordinare-performer:full python3 --version
```

---

## 7. Extend the Base Image for a Custom Stack

Create a custom `Dockerfile`:

```dockerfile
FROM coordinare-performer:base

# Add your project-specific runtime (e.g. Ruby 3.3)
RUN apt-get update && apt-get install -y ruby ruby-dev && rm -rf /var/lib/apt/lists/*
```

Build and use:

```bash
docker build -t coordinare-performer:ruby .
cat dispatch.json | docker run --rm -i -e AGENT_BACKEND=opencode -e ANTHROPIC_API_KEY=... coordinare-performer:ruby
```

---

## Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `AGENT_BACKEND` | `opencode` | AI coding backend to use. Only `opencode` is supported initially. |
| `AGENT_TIMEOUT` | `1800` | Maximum backend runtime in seconds (30 minutes). |
| `ANTHROPIC_API_KEY` | — | API key for Claude-backed opencode (required for opencode backend). |
