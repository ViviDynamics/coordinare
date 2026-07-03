# Quickstart: 123-pipeline-flow-optimizations

Each user story can be verified independently.

## US1 — Tech_writer gate (no-doc-change PR)

```python
# Simulate a PR diff with no docs/ changes
from coordinare.graph.nodes.dispatch_performer import _should_skip_documenting

changed_files = ["src/coordinare/services/qa_verdict.py", "tests/unit/services/test_qa_verdict.py"]
assert _should_skip_documenting(changed_files) is True

changed_files_with_docs = ["src/coordinare/services/qa_verdict.py", "docs/api/qa.md"]
assert _should_skip_documenting(changed_files_with_docs) is False
```

> **Note (as-built):** `PersistedSession` lives in `coordinare.state_store`, not
> `coordinare.graph.state`. The US4 carry-forward field is
> `assessor_open_questions` (a new field distinct from the pre-existing
> `open_questions: list[str]` blocked-card diagnostic surface). US6 lives in
> `route_issue_comments` (owner of `processed_issue_comment_ids`), not
> `check_board`. See tasks.md "Implementation Notes / Deviations".

## US3 — Split bounce budget

```python
from coordinare.state_store import PersistedSession

# Legacy migration: old state has feedback_cycle_count=3, no content_feedback_cycles
session = PersistedSession.model_validate({
    "card_id": "PVTI_123",
    "feedback_cycle_count": 3,
    # content_feedback_cycles absent
})
assert session.content_feedback_cycles == 3  # migrated from legacy field
assert session.transient_error_cycles == 0   # default

# Fresh card
fresh = PersistedSession(card_id="PVTI_456")
assert fresh.content_feedback_cycles == 0
assert fresh.transient_error_cycles == 0
assert fresh.assessor_open_questions == []
```

## US4 — Assessor Q&A carryover

```python
# prior_clarifications is injected into the assessor's card_context payload
# when assessor_open_questions is persisted and the stage is "assessing".
# (dispatch_performer reads state["assessor_open_questions"] at dispatch time.)
from coordinare.graph.nodes.dispatch_performer import dispatch_performer

qa = [{"question": "Should auth use OAuth?", "answer": "Yes, OAuth2 PKCE"}]
# state["assessor_open_questions"] == qa, performer_stage == "assessing"
#   → dispatched card_context["prior_clarifications"] == qa
# state["assessor_open_questions"] == []  → "prior_clarifications" key absent
```

## US6 — Comment dedup before classification

```python
# route_issue_comments filters processed_issue_comment_ids BEFORE the AI
# classifier and skips the call entirely when all comments are processed.
from coordinare.graph.nodes.route_issue_comments import route_issue_comments

# Given 5 fetched comments where 101,102,103 are in processed_issue_comment_ids,
# classify_issue_comment_ai runs exactly twice (for 104 + 105).
# Given all-processed, it runs zero times and the watermark still advances.
```

## Running the test suite

```sh
# From project root (use the venv pytest, not pyenv shim)
.venv/bin/pytest tests/unit/graph/nodes/test_dispatch_performer.py \
                 tests/unit/graph/nodes/test_monitor_performer.py \
                 tests/unit/graph/nodes/test_check_board.py \
                 tests/unit/graph/state/ \
                 -v

# Lint
.venv/bin/ruff check src/coordinare/graph/nodes/dispatch_performer.py \
                     src/coordinare/graph/nodes/monitor_performer.py \
                     src/coordinare/graph/nodes/check_board.py \
                     src/coordinare/graph/state.py \
                     src/coordinare/services/persona_service.py
```
