# Phase 1: Configuration Quickstart

**Audience**: Operators configuring coordinare for multi-project orchestration  
**Reference**: [data-model.md](data-model.md) for entity definitions

## Scenario 1: Single Project (Existing Deployment)

Your `config.yaml` today:

```yaml
github_token: ${GITHUB_TOKEN}
github_org: acme-corp
github_project_number: 42
assignee_filter: coordinare-bot
notification_config:
  slack_webhook: ${SLACK_WEBHOOK}
  slack_channel: "#coordinare"
```

**No changes needed.** Coordinare automatically wraps this as a single symphony named `"default"`. (The `"__default__"` label you may see in metrics is the observability context used when no symphony is actively bound — it is not the config-level symphony name.)

## Scenario 2: Multi-Project Setup

You operate 3 project boards and want coordinare to cycle through all of them with a shared performer pool.

### Project Structure

```
acme-corp org:
├── Frontend Board (project 42)
├── Backend Board (project 43)
└── Infra Board (project 44)
```

### Configuration

**`config.yaml`**:

```yaml
# Global defaults applied to all symphonies
github_token: ${GITHUB_TOKEN}
github_org: acme-corp
assignee_filter: coordinare-bot
notification_config:
  slack_webhook: ${SLACK_WEBHOOK}
  slack_channel: "#coordinare"

# Global performer personas (spec 018)
personas:
  main-persona:
    backend: claude_code
    instructions: |
      You are a senior engineer. Focus on robust solutions.

# List of orchestrated projects (order = priority for performer allocation)
symphonies:
  
  # Backend is highest priority (listed first)
  - name: backend
    github_project_number: 43
    overrides:
      # Override global assignee filter for this symphony
      assignee_filter: coordinare-backend-team
      notification_config:
        slack_channel: "#backend-automation"
    personas:
      # Symphony-specific persona overrides (spec 018)
      main-persona:
        backend: claude_code
        instructions: |
          You specialize in API design and scalability.

  # Frontend is second priority
  - name: frontend
    github_project_number: 42
    # No overrides: uses all global_config values

  # Infra is lowest priority
  - name: infra
    github_project_number: 44
    overrides:
      # Infra cards run less frequently (longer timeout)
      performer_timeout_seconds: 300

# Shared performer pool across all symphonies
orchestra:
  mode: shared_pool
  performers:
    - id: performer-1
      mode: subprocess
      endpoint: null
```

### How This Works

1. **Startup**: Coordinare validates config, creates 3 SymphonyConfig objects
2. **Orchestration Loop** (default 30s):
   - Bind `symphony="backend"` context
   - Poll backend board, dispatch card if needed
   - Clear context
   - Bind `symphony="frontend"` context
   - Poll frontend board, dispatch card if needed
   - Clear context
   - Bind `symphony="infra"` context
   - Poll infra board, dispatch card if needed
   - Clear context
3. **Performer Allocation**: When card needs processing, request from `orchestra.performers` (shared pool)
4. **Metrics**: Each card transition, poll cycle, error is tagged with `symphony=<name>`
5. **Logs**: All events include `symphony=<name>` field in JSON logs

### Dashboard

Navigate to **Symphonies** page:
- **List view**: Shows all 3 symphonies, current active card per symphony
- **Detail view**: Click a symphony name
  - Board snapshot (column counts)
  - Active card details
  - Last N errors (per symphony)
  - Cycle history (per symphony)

## Scenario 3: Per-Symphony Customization

You want each symphony to:
- Use different notifiers (backend → Slack, frontend → email)
- Have different performer timeout behavior
- Run with different personas

```yaml
github_token: ${GITHUB_TOKEN}
github_org: acme
assignee_filter: coordinare-bot
notification_config:
  slack_webhook: ${SLACK_WEBHOOK}

symphonies:
  
  - name: backend
    github_project_number: 43
    overrides:
      performer_timeout_seconds: 300
      notification_config:
        slack_webhook: ${BACKEND_SLACK_WEBHOOK}
        slack_channel: "#backend-coordinare"
    personas:
      main-persona:
        instructions: "You are a systems engineer. Prioritize availability."

  - name: frontend
    github_project_number: 42
    overrides:
      performer_timeout_seconds: 180
      notification_config:
        # No Slack for frontend, use email instead
        smtp_enabled: true
        smtp_to: "frontend-team@acme.com"
```

**Effective Config for Backend**:
```python
ProjectConfiguration(
    github_token="...",
    github_org="acme",
    assignee_filter="coordinare-bot",  # inherited from global
    performer_timeout_seconds=300,     # overridden
    notification_config={
        slack_webhook="...",
        slack_channel="#backend-coordinare"
    },
    personas={...}
)
```

**Effective Config for Frontend**:
```python
ProjectConfiguration(
    github_token="...",
    github_org="acme",
    assignee_filter="coordinare-bot",  # inherited from global
    performer_timeout_seconds=180,     # overridden
    notification_config={
        smtp_enabled=True,
        smtp_to="frontend-team@acme.com"
    },
    personas={...}
)
```

## Scenario 4: Hot-Reload (Dynamic Config Changes)

You want to add a new symphony without restarting coordinare.

### Step 1: Edit config.yaml

Add a new symphony to the list:

```yaml
symphonies:
  - name: backend
    github_project_number: 43
  - name: frontend
    github_project_number: 42
  - name: infra         # NEW SYMPHONY
    github_project_number: 44
```

### Step 2: Trigger Reload

**Via Dashboard**:
- Go to **Admin** → **Configuration**
- Click **Reload Config**
- See validation result

**Via API**:
```bash
curl -X POST http://coordinare:8000/api/config/reload
# Returns:
# {
#   "status": "success",
#   "symphonies_before": 2,
#   "symphonies_after": 3,
#   "added": ["infra"],
#   "removed": [],
#   "modified": []
# }
```

### Step 3: Confirm

Coordinare immediately:
- Validates new config
- Registers new SymphonyRuntimeState for "infra"
- Includes "infra" in next orchestration cycle
- Logs all changes with timestamps

**Note**: If a symphony is removed mid-flight:
- Current card (if any) completes in-flight
- Symphony polling stops next cycle
- Prometheus metrics from removed symphony become stale (kept for historical queries)

## Scenario 5: Unresolved Placeholder Handling

Your `config.yaml` references environment variables:

```yaml
github_token: ${GITHUB_TOKEN}
github_org: ${GITHUB_ORG}

symphonies:
  - name: backend
    github_project_number: ${BACKEND_PROJECT_NUMBER}
```

**At Startup**:
```bash
export GITHUB_TOKEN=ghp_xxx
export GITHUB_ORG=acme
export BACKEND_PROJECT_NUMBER=43
coordinare run
```

**Error Handling**:
```
If ${BACKEND_PROJECT_NUMBER} is unset:
  ✗ Config validation fails at startup
  ✗ Error: "Unresolved placeholder: BACKEND_PROJECT_NUMBER"
  ✗ Coordinare exits with exit code 1
  ✓ Dashboard not available (daemon never started)
```

**Fix**: Set all required environment variables before starting.

## Scenario 6: Invalid Config Detection

**Scenario**: You have duplicate symphony names.

```yaml
symphonies:
  - name: backend
    github_project_number: 43
  - name: backend     # DUPLICATE
    github_project_number: 44
```

**Result**:
```
✗ Validation error: "symphony names must be unique"
✗ Config rejected at startup
✗ Coordinare exits
```

**Fix**: Rename one symphony (e.g., `backend-eu`, `backend-us`).

---

**Scenario**: You have duplicate project numbers.

```yaml
symphonies:
  - name: backend
    github_project_number: 43
  - name: frontend
    github_project_number: 43   # DUPLICATE
```

**Result**:
```
✗ Validation error: "symphony project numbers must be unique"
✗ Config rejected
```

**Fix**: Use correct project number for frontend.

---

**Scenario**: You reference wrong org/project number.

```yaml
github_org: acme-corp

symphonies:
  - name: backend
    github_project_number: 9999  # doesn't exist
```

**At Startup**:
```
✓ Config validates (syntax is correct)
✓ Coordinare starts
✗ First poll cycle: 404 Not Found for org/project/9999
✗ Error logged and recorded in symphony state
✓ Coordinare continues (retries next cycle per resilience spec 005)
✓ Dashboard shows error badge for this symphony
```

**Fix**: Correct the project number and trigger hot-reload.

## Summary: Configuration Rules

| Rule | Example |
|------|---------|
| Symphony names must be unique | ✗ Two symphonies both named "backend" |
| Project numbers must be unique | ✗ Two symphonies referencing project 42 |
| Names must be alphanumeric + dash | ✓ "my-backend", ✗ "my_backend" |
| Overrides override entire field | Overriding `notification_config:` replaces global config entirely |
| Personas merge with global defaults | Symphony persona + global persona = effective persona for symphony |
| Placeholders must be resolved | Set env vars before startup |
| No breaking changes in single-project mode | Existing configs work without modification |

