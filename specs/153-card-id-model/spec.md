# Feature Specification: One card id model

**Feature Branch**: `153-card-id-model`
**Created**: 2026-08-30
**Status**: Draft
**Issue**: #232 (follow-up to spec 149 / #203, merged as 35dd182)

## Context

Spec 149 put a `BoardProvider` protocol in front of coordinare's board so that work could come
from somewhere other than GitHub Projects. Four operations went behind it: poll the board, move
a card, read a card, post a comment on a card. Reading a card's comments did not.

The reason was an id model, not an oversight. Coordinare addresses comment reads by GitHub
**issue number** and every other card operation by **node id** — two identifiers for one thing.
A protocol method promising to take a card id would therefore have been false on GitHub, so
spec 149 removed the method rather than ship a contract it could not honour, and wrote the gap
down.

The gap is real: a team whose board is Jira can hand coordinare cards, statuses, and comment
*writes*, but coordinare cannot read the comments back. Comment routing is how a human redirects
work in flight, so a board that cannot route comments is a board that cannot be steered.

This spec closes it by giving cards one id model.

## Clarifications

- Q: Widen the protocol to take both a card id and an issue number? → A: No. That is the two-id
  model wearing a new coat, and every future provider would have to supply an identifier that
  means nothing to it.
- Q: Have the caller pass the issue number as the card id? → A: No. That is the contract spec
  149 refused to ship, just moved up a layer.
- Q: Resolve the id inside the GitHub adapter? → A: Yes. Translating between coordinare's card id
  and a host's native identifier is exactly what an adapter is for, and it keeps the knowledge
  in the one place that is allowed to know about GitHub.
- Q: Should comment *writes* change too? → A: No. They already take a node id and already work
  through the seam. Only the read is inconsistent.
- Q: Does this cover pull request comments? → A: No. Those are code-host work and stay on the
  code host, as spec 149 decided.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A board that is not GitHub can route card comments (Priority: P1)

A team runs coordinare against a board that is not GitHub Projects. Someone comments on a card to
redirect the work in flight. Coordinare reads that comment and routes it, exactly as it would if
the board were GitHub.

**Why this priority**: This is the whole feature. It is also the last gap in the seam spec 149
built, so until it lands, "coordinare can run on a non-GitHub board" has an asterisk on it.

**Independent Test**: Give coordinare a board provider that knows nothing about GitHub and whose
cards are keyed `PROJ-123`, put a comment on a card, and run the comment-routing step. The
comment is routed, and the code host is never asked for it.

**Acceptance Scenarios**:

1. **Given** a board provider that is not GitHub and a card keyed `PROJ-123`, **When** coordinare
   routes issue comments, **Then** the comments come from that provider and the code-host service
   is never consulted for them.
2. **Given** a card id that is not a number and not a GitHub node id, **When** it is passed to
   the comment read, **Then** nothing rejects it or attempts to interpret its shape.
3. **Given** the board is GitHub Projects, **When** coordinare routes issue comments, **Then** the
   same comments are fetched as before, with the same watermark behaviour.

---

### User Story 2 - The translation costs nothing in the common path (Priority: P2)

An operator's coordinare runs continuously against the GitHub API under a rate limit. Closing this
gap must not spend more of that budget.

**Why this priority**: Independent of US1's correctness, and it is what makes US1 acceptable to
deploy. A working seam that doubles the API calls in the hot loop would be traded away.

**Independent Test**: Route comments for a card that appeared in the most recent board poll and
count the calls the code host receives. There is exactly one — the comment fetch itself.

**Acceptance Scenarios**:

1. **Given** a card present in the most recent board poll, **When** its comments are read,
   **Then** no additional lookup is made to translate its id.
2. **Given** a card that was not in the most recent poll, **When** its comments are read,
   **Then** the translation still succeeds, using a lookup coordinare already knows how to make.
3. **Given** a card whose id cannot be translated at all, **When** its comments are read,
   **Then** coordinare treats it as "no new comments" rather than failing the cycle, matching how
   the comment read already handles failure today.

---

### Edge Cases

- **A card coordinare has never polled.** The in-memory translation is populated by polling. A
  card read before any poll, or after a restart, must still resolve.
- **A stale translation.** Cards do not change identity, so a remembered pairing cannot go wrong;
  it can only be missing. This is why remembering is safe.
- **A board with no separate identifier at all.** Trello and Jira address a card one way. Those
  providers translate nothing, and must not be forced to pretend otherwise.
- **The comment read failing.** Today it returns no comments and logs, rather than failing the
  cycle. That must not change: a board hiccup should not stall the work.
- **Pull request comments.** A different thing that shares a method name on GitHub. It must stay
  where it is.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The board protocol MUST offer reading a card's comments, addressed by the same card
  id as every other board operation.
- **FR-002**: The card id in that operation MUST be opaque. Nothing may require it to be numeric,
  to be a GitHub node id, or to encode a repository.
- **FR-003**: Translation from a card id to a host's native identifier MUST happen inside the
  provider for that host, and nowhere else.
- **FR-004**: The GitHub provider MUST perform that translation without additional API calls for
  any card seen in the most recent board poll.
- **FR-005**: The GitHub provider MUST still resolve a card that was not in the most recent poll.
- **FR-006**: A card that cannot be resolved MUST yield no comments rather than an error that
  stops the cycle, matching today's failure behaviour.
- **FR-007**: Comment routing MUST obtain comments through the board, not through the code host.
- **FR-008**: Reading a *pull request's* comments MUST remain code-host work and MUST be
  unchanged.
- **FR-009**: The comments returned MUST be the same, in the same shape, with the same watermark
  semantics, as before this change.
- **FR-010**: A provider that has only one identifier for a card MUST be able to implement the
  operation with no translation step.
- **FR-011**: The record MUST be corrected: the documented statement that comment routing is
  GitHub-bound, and the tests asserting the operation is absent, describe a state that no longer
  exists and MUST be updated to assert what is now true.

### Key Entities

- **Card id**: the one identifier coordinare uses for a card, across every board operation.
  Opaque, provider-defined.
- **Native identifier**: what a particular host calls the same card. Known only to that host's
  provider. On GitHub Projects there are two — a node id and an issue number — and reconciling
  them is this spec.
- **Identifier map**: the pairing between the two, already produced by polling the board, and
  remembered so translation is free.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A board provider containing no GitHub knowledge routes card comments through a real
  coordinare cycle, with the code-host service asserted unused for that read.
- **SC-002**: A card key of the form `PROJ-123` works end to end.
- **SC-003**: Reading comments for a polled card makes exactly one request of the code host: the
  comment fetch itself.
- **SC-004**: The board protocol gains exactly one operation, and no operation on it takes a
  GitHub-shaped argument.
- **SC-005**: Pull request comment reading is byte-identical to before.
- **SC-006**: No test outside this feature's own file and the comment-routing tests is modified —
  the same evidence spec 149 used that behaviour did not change.
- **SC-007**: No statement remains in the code or specs describing comment routing as bound to
  the code host.

## Out of Scope

- Any non-GitHub provider implementation. This spec makes one possible; it does not write one.
- The advocate's and the bench's own code-host services, which hold their own rather than reading
  graph state.
- Pull request comments, reviews, and every other code-host operation.
- Changing which comments coordinare acts on, or how it classifies them.
