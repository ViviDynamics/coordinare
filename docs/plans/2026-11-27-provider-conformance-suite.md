# Provider conformance suite and an in-memory work item provider

Issue #400 (part of epic #393)

## Scope
In:
- `InMemoryBoardProvider`: a work item provider with string ids (`PAY-1`), its own
  native state names, string comment cursors, optional workflow rules that refuse a
  transition, and no issue numbers or node ids anywhere.
- One conformance suite, parameterised over providers, run against GitHub Projects
  (the adapter over the bench's `FakeGitHubService`) and the in-memory provider.
  It covers listing by canonical state (including an unmapped native state), a
  refused move, a person's move during a run, comments after a cursor, a repeated
  write, and restart reconcile.
- The board bench runs once through the in-memory provider and drives one item
  from To Do to Done.

Out:
- Provider selection in config (#394), typed work items (#395), routing every
  write through the provider (#396), work item references (#397). This issue
  proves the seam with the operations it has today.
- Coordinare-side string comment cursors (#398). The in-memory provider issues
  string cursors and the suite passes them back opaquely; coordinare's own
  persisted cursor stays an integer until #398.

## Assumptions
- The GitHub Projects adapter accepts the card id from the listing for every
  operation. Today `get_card` and `add_card_comment` only work with the issue
  node id. The adapter translates a listed card id to its node id from the
  pairing the last poll already fetched, and passes any other id through
  unchanged, so existing callers that pass node ids see no change.
- The bench fake mirrors the real service where the suite needs it: an unmapped
  status is left out of the listing, and a person's comment on a card can be read
  back by card.
- GitHub Projects has no workflow rules, so the refused move case is declared
  as a capability and skipped for it with that reason.
- Restart reconcile reads a configured provider that is not a GitHub project
  without requiring a GitHub project id. GitHub Projects keeps today's gate.

## Tasks
- [x] 1. Conformance suite with the in-memory provider: listing, moves, refusal,
  a person's move, comments, repeated writes, restart reconcile.
- [x] 2. GitHub Projects in the suite through `FakeGitHubService`.
- [x] 3. Bench: one item from To Do to Done through the in-memory provider.
