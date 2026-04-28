# Contract: Assessor Comment Routing

**Module**: Extension to existing assessor node in `src/coordinare/graph/nodes/`

## Interface

```python
async def classify_comment(
    comment: IssueCommentEvent | PRCommentEvent,
    state: CoordinareState,
) -> CommentClassification:
    """Classify a single comment from any source (PR or issue).

    Uses LLM or rule-based logic to assign one of the defined labels.
    Never raises — returns classification=noise on error.
    """
```

## Classification Labels

| Label | Trigger | Downstream action |
|-------|---------|-------------------|
| `clarification` | Question about behavior, requirements, or scope | Append to `card_clarifications`; optionally notify performer |
| `scope_change` | Request to add, modify, or remove a feature | Set `requirements_changed=True`; notify performer |
| `blocker_update` | Comment indicates a dependency resolved or new blocker | Log; operator notified; may unblock session |
| `approval` | LGTM / approved / looks good | Record; no performer action |
| `noise` | Bot comment, emoji-only, greeting, out-of-scope | Record; no performer action |

## Idempotency

The caller maintains `processed_comment_ids: set[int]` on the session. `classify_comment` MUST NOT be called for an ID already in that set. After classification, the caller adds the ID to the set.

## Source Agnosticism

The `source` field (`pr` | `issue`) is metadata for logging/observability only. Classification logic MUST be identical regardless of source. The same comment text on a PR and on an issue MUST produce the same classification label.

## Error Handling

If the LLM call or rule engine fails, return `CommentClassification(classification="noise", summary="classification_error")` and log the error. Never raise from `classify_comment`.
