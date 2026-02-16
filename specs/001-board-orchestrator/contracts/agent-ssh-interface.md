# Contract: Agent SSH Interface

**Feature**: 001-board-orchestrator
**Date**: 2026-02-16
**Service**: Project Agent (remote, accessed via SSH)
**Client**: `src/coordinare/services/agent_ssh.py`
**Requirement**: FR-019

## Connection

The coordinare connects to the agent host via SSH using `asyncssh`.

```
Host: {agent_host}:{agent_port}
User: {agent_user}
Key:  {agent_key_path}
```

All parameters sourced from `ProjectConfiguration`.

---

## Commands

The coordinare invokes the agent via CLI commands over SSH. The `agent_command` in config is a template string with placeholders.

### C1: Dispatch Card

Start work on a new card. Sends card context as a JSON payload via stdin.

```bash
{agent_command} dispatch --card-context '{card_context_json}'
```

**Card Context JSON Schema**:
```json
{
  "card_id": "string",
  "issue_number": 123,
  "title": "string",
  "description": "string (markdown)",
  "acceptance_criteria": ["string", "..."],
  "issue_url": "https://github.com/org/repo/issues/123",
  "labels": ["string", "..."]
}
```

**Expected Response** (stdout, JSON):
```json
{
  "status": "accepted",
  "session_id": "string"
}
```

**Error Response**:
```json
{
  "status": "rejected",
  "reason": "string"
}
```

**Timeout**: 60 seconds. On timeout, retry once, then move card to BLOCKED.

---

### C2: Check Agent Status

Poll the agent's progress on the current card.

```bash
{agent_command} status --session-id '{session_id}'
```

**Expected Response** (stdout, JSON):
```json
{
  "status": "working" | "pr_opened" | "blocked" | "error",
  "pr_url": "string (optional, when status=pr_opened)",
  "questions": ["string (optional, when status=blocked)"],
  "error_message": "string (optional, when status=error)",
  "commits": [
    {
      "sha": "string",
      "message": "string"
    }
  ]
}
```

**Timeout**: 30 seconds. On timeout, log warning and retry on next poll cycle.

---

### C3: Relay Feedback

Send human review feedback to the agent for remediation.

```bash
{agent_command} feedback --session-id '{session_id}' --feedback '{feedback_json}'
```

**Feedback JSON Schema**:
```json
{
  "reviewer": "string (GitHub login)",
  "review_state": "CHANGES_REQUESTED" | "COMMENTED",
  "body": "string",
  "required_changes": [
    {
      "file": "string (optional)",
      "description": "string",
      "severity": "blocking" | "suggestion" | "nit"
    }
  ]
}
```

**Expected Response** (stdout, JSON):
```json
{
  "status": "acknowledged",
  "session_id": "string"
}
```

**Timeout**: 30 seconds.

---

### C4: Health Check

Verify the agent is reachable and operational.

```bash
{agent_command} health
```

**Expected Response** (stdout, JSON):
```json
{
  "status": "healthy" | "unhealthy",
  "version": "string",
  "uptime_seconds": 12345
}
```

**Timeout**: 10 seconds. Used at startup (Acceptance Scenario 1.5) and by the health-check endpoint.

---

## Error Handling

| Scenario | Coordinare Action |
|----------|-----------------|
| SSH connection refused | Retry with exponential backoff (max 3 attempts), then move card to BLOCKED, notify team |
| Agent command timeout | Retry once, then log warning; on consecutive timeouts, move card to BLOCKED |
| Agent returns `status: error` | Move card to BLOCKED, post error details as comment, notify team |
| Agent returns `status: blocked` | Move card to BLOCKED, post agent's questions as comment, notify team |
| Invalid JSON response | Log error, retry once, then move card to BLOCKED |
| SSH key authentication failure | Fail startup with clear error message (do not retry) |
