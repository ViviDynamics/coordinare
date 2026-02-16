# Data Model: Board Orchestrator Daemon

**Feature**: 001-board-orchestrator
**Date**: 2026-02-16
**Source**: [spec.md](./spec.md) Key Entities + Functional Requirements

## Entities

### 1. ProjectConfiguration

The coordinare's settings for a single project. Loaded once at startup from config file + env vars (FR-015).

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `project_name` | `str` | Yes | Human-readable project name |
| `github_org` | `str` | Yes | GitHub organization login |
| `github_project_number` | `int` | Yes | GitHub Projects v2 number |
| `github_token` | `SecretStr` | Yes | GitHub PAT or App installation token |
| `agent_host` | `str` | Yes | SSH hostname for agent dispatch |
| `agent_port` | `int` | No | SSH port (default: 22) |
| `agent_user` | `str` | Yes | SSH username for agent host |
| `agent_key_path` | `Path` | No | Path to SSH private key (default: ~/.ssh/id_ed25519) |
| `agent_command` | `str` | Yes | CLI command template for agent invocation |
| `human_reviewers` | `list[str]` | Yes | GitHub logins of authorized human reviewers |
| `notification_email` | `str` | Yes | Email address for notifications (default: coordinare@vividynamics.com) |
| `smtp_host` | `str` | Yes | SMTP server hostname |
| `smtp_port` | `int` | No | SMTP port (default: 587) |
| `smtp_username` | `str` | No | SMTP authentication username |
| `smtp_password` | `SecretStr` | No | SMTP authentication password |
| `slack_webhook_url` | `SecretStr` | Yes | Slack incoming webhook URL |
| `slack_channel` | `str` | Yes | Slack channel for notifications |
| `poll_interval_seconds` | `int` | No | Board polling interval (default: 30) |
| `blocked_reminder_hours` | `int` | No | Hours between blocked-card reminder notifications (default: 24) |
| `health_check_port` | `int` | No | Port for health/metrics endpoint (default: 8080) |

**Validation Rules**:
- `human_reviewers` must contain at least one entry
- `poll_interval_seconds` must be between 10 and 300
- `github_token` must be non-empty
- `agent_command` must contain `{card_context}` placeholder

**Configuration Layering** (FR-015):
1. Base: YAML/TOML config file (mounted via Docker volume / Kubernetes ConfigMap)
2. Override: Environment variables with `COORDINARE_` prefix (e.g., `COORDINARE_GITHUB_TOKEN`)
3. Env vars always take precedence over config file values

---

### 2. Card

A work item on the board. Represents an issue or draft issue from GitHub Projects.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | `str` | Yes | GitHub Projects item ID (GraphQL node ID) |
| `issue_id` | `str` | Yes | GitHub Issue node ID |
| `issue_number` | `int` | Yes | GitHub Issue number |
| `title` | `str` | Yes | Card title |
| `description` | `str` | Yes | Card body/description (Markdown) |
| `acceptance_criteria` | `list[str]` | No | Parsed from issue body (## Acceptance Criteria section) |
| `status` | `CardStatus` | Yes | Current board column |
| `previous_status` | `CardStatus` | No | Previous column (for transition tracking) |
| `assigned_agent` | `str` | No | Agent host handling this card |
| `pr_url` | `str` | No | URL of linked pull request |
| `pr_node_id` | `str` | No | GraphQL node ID of linked PR |
| `open_questions` | `list[str]` | No | Questions posted by coordinare when blocked |
| `transition_history` | `list[CardTransition]` | No | History of column transitions |
| `created_at` | `datetime` | Yes | When the card was first seen by coordinare |
| `updated_at` | `datetime` | Yes | Last status change timestamp |

**Validation Rules**:
- `title` must be non-empty
- `status` must be a valid `CardStatus` value
- `pr_url` required when `status` is `IN_REVIEW`
- `open_questions` required when `status` is `BLOCKED`

---

### 3. CardStatus (Enum)

Maps to GitHub Projects board columns (FR-016).

| Value | Board Column | Description |
|-------|-------------|-------------|
| `TODO` | ToDo / Backlog | Card waiting to be picked up |
| `BLOCKED` | Blocked | Card needs input from engineering team |
| `IN_PROGRESS` | In Progress | Card dispatched to agent, work underway |
| `IN_REVIEW` | In Review | PR open, awaiting human review |
| `DONE` | Done | PR merged, card complete |

---

### 4. CardTransition

Records a single column transition for audit/notification purposes.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `from_status` | `CardStatus` | Yes | Previous column |
| `to_status` | `CardStatus` | Yes | New column |
| `timestamp` | `datetime` | Yes | When the transition occurred |
| `reason` | `str` | No | Why the transition happened (e.g., "PR approved", "agent needs input") |

---

### 5. Review

Feedback on a PR associated with a card.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | `str` | Yes | GitHub review node ID |
| `author_login` | `str` | Yes | GitHub login of the reviewer |
| `author_type` | `ReviewerType` | Yes | `HUMAN` or `BOT` |
| `state` | `ReviewState` | Yes | `APPROVED`, `CHANGES_REQUESTED`, `COMMENTED`, `DISMISSED` |
| `body` | `str` | No | Review comment text |
| `submitted_at` | `datetime` | Yes | When the review was submitted |
| `is_actionable` | `bool` | Yes | Whether this review should trigger coordinare action |

**Classification Logic** (FR-005, FR-006):
- `author_type = HUMAN` if `author_login` is in `ProjectConfiguration.human_reviewers`
- `author_type = BOT` otherwise (catches CoPilot, Dependabot, GitHub Actions, etc.)
- `is_actionable = True` only when `author_type == HUMAN`

---

### 6. ReviewerType (Enum)

| Value | Description |
|-------|-------------|
| `HUMAN` | Configured human team member |
| `BOT` | Automated reviewer (CoPilot, Dependabot, etc.) |

---

### 7. ReviewState (Enum)

| Value | Description |
|-------|-------------|
| `APPROVED` | Reviewer approved the PR |
| `CHANGES_REQUESTED` | Reviewer requested changes |
| `COMMENTED` | Reviewer left general comments |
| `DISMISSED` | Review was dismissed |

---

### 8. Notification

A message sent on card transitions (FR-009, FR-010, FR-011).

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `card_title` | `str` | Yes | Title of the card |
| `card_status` | `CardStatus` | Yes | Current status after transition |
| `previous_status` | `CardStatus` | Yes | Status before transition |
| `task_description` | `str` | Yes | Card description summary |
| `open_questions` | `list[str]` | No | Questions needing answers (if blocked) |
| `commit_summary` | `str` | No | Summary of commits (if PR exists) |
| `pr_url` | `str` | No | PR link (if applicable) |
| `timestamp` | `datetime` | Yes | When the notification was generated |

**Delivery Channels**:
- Email to `ProjectConfiguration.notification_email` (FR-009)
- Slack to `ProjectConfiguration.slack_channel` (FR-010)

**Content Rules** (FR-011):
- Always includes: status, task description
- Includes open questions when transitioning to BLOCKED
- Includes commit summary and PR link when transitioning to IN_REVIEW or DONE

---

### 9. CoordinareState (LangGraph State Schema)

The stateful context carried across graph invocations via checkpointing.

| Field | Type | Reducer | Description |
|-------|------|---------|-------------|
| `current_card` | `Card \| None` | overwrite | The card currently being processed |
| `board_snapshot` | `dict[CardStatus, list[str]]` | overwrite | Map of column → list of item IDs |
| `phase` | `str` | overwrite | Current daemon phase: `idle`, `dispatching`, `monitoring_agent`, `monitoring_pr`, `blocked`, `merging` |
| `pending_reviews` | `list[Review]` | overwrite | Unprocessed reviews on current PR |
| `last_poll_at` | `datetime` | overwrite | Timestamp of last board poll |
| `error_count` | `int` | overwrite | Consecutive error count for backoff |
| `github_field_cache` | `dict` | overwrite | Cached Status field ID and option IDs |

**Persistence**: Checkpointed after every node execution via LangGraph's `PostgresSaver` (production) or `MemorySaver` (test). Recovered on daemon restart via stable `thread_id`.

---

## State Transition Diagram

```
                    ┌─────────────┐
                    │  TODO       │
                    └──────┬──────┘
                           │ pick_next (no cards in IN_PROGRESS or IN_REVIEW)
                           ▼
                    ┌──────────────┐
          ┌────────│ IN_PROGRESS   │────────┐
          │        └──────┬────────┘        │
          │               │                 │
          │  agent needs  │ agent opens PR  │ card insufficient
          │  input        │                 │ (FR-012)
          │               ▼                 │
          │        ┌──────────────┐         │
          │        │  IN_REVIEW   │         │
          │        └──────┬───────┘         │
          │               │                 │
          │    ┌──────────┼──────────┐      │
          │    │          │          │      │
          │  approved  feedback   conflict  │
          │    │      requested     │       │
          │    ▼          │         │       │
          │  ┌──────┐    │    ┌────┴──┐    │
          │  │ DONE │    │    │BLOCKED│◄───┘
          │  └──────┘    │    └───┬───┘
          │              │        │
          │              ▼        │ team answers questions
          │        relay to       │
          │        agent ─────────┘
          │              │
          └──────────────┘
                 (moves to BLOCKED)
```

### Valid Transitions

| From | To | Trigger | FR |
|------|----|---------|-----|
| TODO | IN_PROGRESS | No cards in IN_PROGRESS or IN_REVIEW; card assessed as sufficient | FR-003, FR-012 |
| TODO | BLOCKED | Card details insufficient for confident execution | FR-012 |
| IN_PROGRESS | IN_REVIEW | Agent opens PR for the card | FR-005 |
| IN_PROGRESS | BLOCKED | Agent needs input to continue | FR-012 |
| IN_REVIEW | DONE | Human reviewer approves PR; coordinare squash-merges | FR-008 |
| IN_REVIEW | BLOCKED | Merge conflict detected | Edge case |
| IN_REVIEW | IN_PROGRESS | Human reviewer requests changes; feedback relayed to agent | FR-007 |
| BLOCKED | IN_PROGRESS | Team provides answers to open questions | FR-014 |
| BLOCKED | TODO | Card returned to backlog (manual team decision) | Edge case |

### Transition Invariants
- At most ONE card in IN_PROGRESS or IN_REVIEW at any time (FR-002)
- Every transition triggers email + Slack notification (FR-009, FR-010)
- BLOCKED transitions always include specific questions on the card (FR-013)
