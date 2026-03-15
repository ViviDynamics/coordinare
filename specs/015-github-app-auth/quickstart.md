# Quickstart: 015 — GitHub App Auth, Polling Config & Webhooks

**Branch**: `015-github-app-auth` | **Date**: 2026-03-12

---

## What this feature adds

1. **GitHub App auth mode** — run Coordinare as a bot identity (`coordinare[bot]`) with automatic JWT generation and token refresh.
2. **Configurable polling** — set `poll_interval_seconds: 0` to disable timer-based polling entirely.
3. **Optional webhook endpoint** — receive GitHub events in real time; wake the daemon cycle immediately.

---

## New dependency

```toml
# pyproject.toml [project.dependencies]
"PyJWT[crypto]>=2.8",
```

---

## Config examples

### PAT mode (unchanged, backwards-compatible)

```yaml
github_auth: pat          # default — can be omitted
github_token: ghp_...
poll_interval_seconds: 30
```

### GitHub App mode

```yaml
github_auth: app
github_app_id: 12345
github_private_key_path: /secrets/coordinare.pem
github_installation_id: 67890
poll_interval_seconds: 30
```

### Disable polling, use webhooks only

```yaml
github_auth: app
github_app_id: 12345
github_private_key_path: /secrets/coordinare.pem
github_installation_id: 67890

poll_interval_seconds: 0   # disables timer

webhooks:
  enabled: true
  secret: whsec_changeme
  path: /webhook/github    # default
```

### Webhooks + polling together

```yaml
poll_interval_seconds: 120   # slow background poll as fallback

webhooks:
  enabled: true
  secret: whsec_changeme
```

---

## New module: `src/coordinare/auth/`

| File | Purpose |
|---|---|
| `protocol.py` | `GitHubAuth` Protocol definition |
| `pat.py` | `PatAuth` — static token wrapper |
| `app.py` | `AppAuth` — JWT generation, token exchange, refresh |
| `__init__.py` | Public exports + `build_auth(config)` factory |

---

## Key callsite changes

**`coordinare.py` / `__main__.py`** (startup):
```python
from coordinare.auth import build_auth
auth = build_auth(config)
github_service = GitHubService(auth=auth, org=..., project_number=...)
```

**`GitHubService.__init__`**:
```python
def __init__(self, auth: GitHubAuth, org: str, project_number: int, ...)
```
Every method that makes a GitHub API call calls `token = await self._auth.get_token()` and uses it to build (or rebuild) the GQL transport.

**`CoordinareDaemon.__init__`** — new parameter:
```python
webhook_trigger: asyncio.Event | None = None
```

---

## Testing strategy

| Layer | What to test |
|---|---|
| `PatAuth` unit | get_token returns token; empty token raises at construction |
| `AppAuth` unit | JWT generation (structure, claims, RS256 alg); token caching; refresh triggers at <5min; concurrent calls serialised; file-not-found at construction; transient HTTP error propagates as TransientGitHubError |
| Config unit | PAT mode: token required; App mode: app_id/key/installation_id required; poll=0 valid; webhook secret required when enabled; backoff validator skips when poll=0 |
| Daemon unit | poll=0 blocks on webhook_trigger; poll>0 wakes early on trigger; trigger cleared after each wake; concurrent-webhook-during-cycle queues next cycle |
| Webhook unit | valid signature returns 200 + sets event; invalid signature returns 401 + event NOT set; missing header returns 401 |
| Integration | AppAuth against GitHub sandbox (CI: skip if credentials absent); Full daemon cycle triggered by webhook mock |

---

## Startup validation errors

| Misconfiguration | Error message |
|---|---|
| `github_auth: pat` with no `github_token` | `"github.auth=pat requires github.token to be set"` |
| `github_auth: app` with missing field | `"github.auth=app requires github_app_id, github_private_key_path, and github_installation_id"` |
| `github_private_key_path` file not found | `"github_private_key_path '/path/key.pem' does not exist or is not readable"` |
| `webhooks.enabled=true` with no secret | `"webhooks.secret is required when webhooks.enabled=true"` |
| `poll_interval_seconds=0` + `webhooks.enabled=false` | `WARNING: "no trigger source configured — coordinare will not process any cycles"` (starts anyway) |
