# Data Model: One card id model (spec 153)

No persisted state changes. No `state_store.py` schema bump. Everything here is in-memory and
per-process.

## Card id

The single identifier coordinare uses for a card, in every board operation.

- **Type**: opaque string.
- **Rule**: nothing outside a provider may interpret its shape. Not parsed, not cast to int, not
  assumed to be a GitHub node id, not assumed to encode a repository.
- **Examples**: `PVTI_lADO...` (GitHub Projects item), `PROJ-123` (Jira), a Trello card id.

## Identifier map (GitHub only)

`{card_id -> issue_number}`, held by `GitHubService`.

- **Populated by**: `poll_board()`, from the `issue_numbers` it already computes.
- **Also populated by**: a successful fallback resolution, so the cost is paid at most once per
  card.
- **Lifetime**: the service instance. Per symphony, because each symphony holds its own service.
- **Never invalidated**: a card's identity does not change, so an entry cannot become wrong. It
  can only be absent, which the fallback handles. This is the property that makes remembering
  safe without a TTL.
- **Not persisted**: rebuilt by the first poll after a restart; the fallback covers the window
  before it.

## Card comment (crosses the protocol boundary)

```
{"id": int, "author": str, "body": str, "created_at": str}
```

Already the shape `GitHubService.get_issue_comments` produces (github.py:1063-1068) — it
normalises GitHub's `user.login` down to `author`. Unchanged by this spec; recorded here because
it is what any future provider must return.

## IssueCommentEvent (unchanged)

`issue_number, comment_id, author, body, created_at, card_id`. Constructed by coordinare from the
comment above. `issue_number` is retained as metadata; see research R6.
