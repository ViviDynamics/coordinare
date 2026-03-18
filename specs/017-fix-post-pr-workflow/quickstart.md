# Quickstart: Post-PR Workflow Bug Fixes

**Feature**: 017-fix-post-pr-workflow
**Date**: 2026-03-16

This document describes how to verify each bug fix in isolation using unit tests.

---

## Scenario 1 — PR opened: GitHub board moves to IN_REVIEW

**File**: `tests/unit/graph/nodes/test_monitor_agent.py`

```python
# Given: agent reports pr_opened with valid pr_url and pr_node_id
state["agent_service"] = _Agent({
    "status": "pr_opened",
    "pr_url": "https://github.com/org/repo/pull/1",
    "pr_node_id": "PR_NODE_1",
})
state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
state["github_service"] = gh  # tracks move_card calls

# When
result = await monitor_agent(state)

# Then: GitHub board is updated AND phase transitions
assert ("ITEM_1", "IN_REVIEW") in gh.move_calls
assert result["phase"] == "monitoring_pr"
```

---

## Scenario 2 — Session expired WITH existing PR → resume monitoring

**File**: `tests/unit/graph/nodes/test_monitor_agent.py`

```python
# Given: session expired but pr_node_id is already set (PR was opened)
state["agent_service"] = _Agent({"status": "session_expired"})
state["current_card"] = {"id": "ITEM_1", "pr_node_id": "PR_NODE_1"}
state["github_service"] = gh

# When
result = await monitor_agent(state)

# Then: routes to monitoring_pr, does NOT move card to TODO
assert result["phase"] == "monitoring_pr"
assert ("ITEM_1", "TODO") not in gh.move_calls
```

---

## Scenario 3 — Session expired WITHOUT existing PR → requeue to TODO (existing behaviour preserved)

**File**: `tests/unit/graph/nodes/test_monitor_agent.py`

```python
# Given: session expired and no pr_node_id
state["agent_service"] = _Agent({"status": "session_expired"})
state["current_card"] = {"id": "ITEM_1"}  # no pr_node_id
state["github_service"] = gh

# When
result = await monitor_agent(state)

# Then: existing behaviour — card requeued to TODO
assert result["phase"] == "idle"
assert ("ITEM_1", "TODO") in gh.move_calls
```

---

## Scenario 4 — Open questions preserved on session expiry

**File**: `tests/unit/graph/nodes/test_monitor_agent.py`

```python
# Given: session expired with open questions in state
state["agent_service"] = _Agent({"status": "session_expired"})
state["current_card"] = {"id": "ITEM_1"}
state["open_questions"] = ["What API key format?", "Which region?"]

# When
result = await monitor_agent(state)

# Then: questions saved to card_clarifications before clearing
assert result["open_questions"] == []
clarifications = result["card_clarifications"]
assert len(clarifications) == 1
assert "What API key format?" in clarifications[0]["questions"]
assert clarifications[0]["answer"] == ""
```

---

## Scenario 5 — Workspace missing token → dispatch aborted, card blocked

**File**: `tests/unit/graph/nodes/test_dispatch_card.py`

```python
# Given: workspace manager returns WorkspaceInfo with empty github_token
class _WMEmptyToken:
    async def prepare(self, card):
        from coordinare.workspace import WorkspaceInfo
        from pathlib import Path
        return WorkspaceInfo(
            path=Path("/tmp/ws"),
            branch="coordinare/ITEM_1/test",
            repo_url="https://github.com/org/repo.git",
            github_token="",  # empty!
        )

state["workspace_manager"] = _WMEmptyToken()
state["agent_service"] = healthy_agent
state["github_service"] = gh
state["current_card"] = {"id": "ITEM_1"}

# When
result = await dispatch_card(state)

# Then: card blocked, performer never dispatched
assert result["phase"] == "blocked"
assert ("ITEM_1", "BLOCKED") in gh.move_calls
assert any("github_token" in q for q in result["open_questions"])
```

---

## Scenario 6 — Workspace unexpected exception → dispatch aborted, card blocked

**File**: `tests/unit/graph/nodes/test_dispatch_card.py`

```python
# Given: workspace manager raises an unexpected exception (not WorkspaceSetupError)
class _WMBrokenPrepare:
    async def prepare(self, card):
        raise RuntimeError("Unexpected git internal error")

state["workspace_manager"] = _WMBrokenPrepare()
state["agent_service"] = healthy_agent
state["github_service"] = gh
state["current_card"] = {"id": "ITEM_1"}

# When
result = await dispatch_card(state)

# Then: card blocked, performer never dispatched
assert result["phase"] == "blocked"
assert ("ITEM_1", "BLOCKED") in gh.move_calls
```

---

## Running the tests

```bash
# Run just the affected unit test files
.venv/bin/pytest tests/unit/graph/nodes/test_monitor_agent.py tests/unit/graph/nodes/test_dispatch_card.py -v

# Run the full suite with coverage
.venv/bin/pytest --cov=coordinare --cov-report=term-missing
```
