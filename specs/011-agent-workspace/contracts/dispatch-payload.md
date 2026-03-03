# Contract: Dispatch Payload (011 additions)

This document extends the existing `dispatch` action payload defined in spec 004 (agent-protocol). The fields below are **additive** — existing agents that ignore unknown payload fields will continue to work.

## Dispatch Action Payload (extended)

```json
{
  "action": "dispatch",
  "session_id": "",
  "payload": {
    "title": "string",
    "description": "string",
    "acceptance_criteria": ["string"],
    "board_card_id": "string",
    "column": "string",

    "repo_url": "string",
    "branch": "string",
    "workspace_path": "string"
  }
}
```

### New Fields

| Field | Type | Required | Transport | Description |
|-------|------|----------|-----------|-------------|
| `repo_url` | `string` | Yes | All | Plain HTTPS URL: `https://github.com/{org}/{project}.git`. No token embedded. |
| `branch` | `string` | Yes | All | Deterministic branch name: `coordinare/{card_id}/{title_slug}`. Already checked out in the workspace. |
| `workspace_path` | `string` | No | subprocess, SSH | Absolute path to the cloned workspace directory on the coordinare host. Omitted for Kubernetes transport. |

### Notes

- `workspace_path` is an absolute path that the performer subprocess can use as its working directory directly (e.g., `cwd=payload["workspace_path"]`).
- The branch at `workspace_path` is already checked out and clean. No further `git checkout` is needed.
- Git credentials are pre-configured in the workspace's local `.git/config`; `git push` works without additional credential setup.
- For Kubernetes transport, `workspace_path` is absent. The performer container is expected to clone `repo_url` and check out `branch` itself, using credentials supplied via K8s Secrets (managed by the operator, not by this feature).

## Example: Subprocess Transport

```json
{
  "action": "dispatch",
  "session_id": "",
  "payload": {
    "title": "Add retry logic to payment service",
    "description": "The payment service times out under load...",
    "acceptance_criteria": ["Retries up to 3 times", "Exponential backoff"],
    "board_card_id": "PVTI_abc123",
    "column": "TODO",
    "repo_url": "https://github.com/acme/payments.git",
    "branch": "coordinare/PVTI_abc123/add-retry-logic-to-payment-se",
    "workspace_path": "/tmp/coordinare-ws-a1b2c3/repo"
  }
}
```

## Example: Kubernetes Transport

```json
{
  "action": "dispatch",
  "session_id": "",
  "payload": {
    "title": "Add retry logic to payment service",
    "description": "...",
    "acceptance_criteria": ["..."],
    "board_card_id": "PVTI_abc123",
    "column": "TODO",
    "repo_url": "https://github.com/acme/payments.git",
    "branch": "coordinare/PVTI_abc123/add-retry-logic-to-payment-se"
  }
}
```
