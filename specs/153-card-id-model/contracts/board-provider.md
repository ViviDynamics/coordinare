# Contract: BoardProvider, after spec 153

## The added operation

```
async def get_card_comments(card_id: str, since_id: int | None = None) -> list[dict[str, Any]]
```

- `card_id` is **opaque**. A provider may interpret it; no caller may.
- `since_id` is a watermark: return only comments after it. `None` means all.
- Returns comments oldest-first, each `{"id": int, "author": str, "body": str, "created_at": str}`.
- Returns `[]` when the card cannot be resolved or the read fails. It MUST NOT raise for those:
  a board hiccup must not stall the cycle. This matches the behaviour that already exists.

## Full surface after this spec (five operations)

| Operation | Takes | Returns |
|---|---|---|
| `poll_board` | — | board snapshot |
| `move_card` | card id, canonical status | `MoveOutcome` |
| `get_card` | card id | card detail |
| `add_card_comment` | card id, plain-text body | posted comment |
| `get_card_comments` | card id, watermark | comments, oldest first |

No operation takes an `owner`, a `repo`, or an `issue_number`. That is asserted, not asserted-by-
convention: the spec-149 test inspects the signatures.

## GitHub adapter obligations

- `poll_board()` remembers the card-id → issue-number pairing it already receives.
- `get_card_comments(card_id, since_id)` resolves the pairing from memory; on a miss it resolves
  once via issue details and remembers the result; if it still cannot resolve, it returns `[]`.
- The translation is entirely inside the adapter and the service. No caller learns that GitHub
  has two identifiers.

## Obligations for a provider with one identifier

Implement `get_card_comments` directly. No translation step, no pretend issue number, nothing to
stub out.
