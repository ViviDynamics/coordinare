# Tasks: One card id model

**Feature**: `153-card-id-model` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)
**Issue**: #232

Tests come before implementation (Constitution II, NON-NEGOTIABLE). The design decisions in
[research.md](./research.md) are settled — R1 in particular: the identifier map lives on
`GitHubService`, never on the adapter.

## Phase 1: Setup

- [x] T001 Create `tests/unit/test_153_card_id_model.py` with the module docstring stating what
      the file proves, and a `_RecordingGitHub` fake that appends **every** method call name to a
      list (research R4 — not `AsyncMock`, whose auto-created attributes make a typo'd assertion
      pass). Methods: `poll_board`, `get_issue_comments`, `get_issue_details`.

## Phase 2: Foundational — the translation (blocking both stories)

- [x] T002 (FR-004) In `src/coordinare/services/github.py`, have `poll_board()` remember the
      `issue_numbers` map it already builds at lines 861-897, on the service instance. Additive
      only: the returned dict is unchanged, so no caller sees a difference.
- [x] T003 (FR-005, FR-006) In `src/coordinare/services/github.py`, add
      `async def issue_number_for_card(self, card_id: str) -> int | None`: return the remembered
      pairing; on a miss resolve via `get_issue_details(card_id)` and read `number`, remembering
      the result so the cost is paid once per card (research R2); return `None` when it cannot be
      resolved, never raising.

**Checkpoint**: GitHub can translate a card id to an issue number without an extra call for any
polled card.

---

## Phase 3: User Story 1 — a non-GitHub board routes card comments (P1) 🎯 MVP

**Goal**: comment routing works against a board that knows nothing about GitHub.

**Independent test**: drive the real `route_issue_comments` node with a provider whose cards are
keyed `PROJ-123`; comments route, and the code-host service is never consulted for the read.

### Tests (write first, expect failure)

- [x] T004 [P] [US1] (FR-002, SC-004) In `tests/unit/test_153_card_id_model.py`, assert the protocol surface is
      exactly five operations and that none takes an `owner`, `repo`, or `issue_number`
      parameter — asserted by inspecting signatures, as spec 149 does.
- [x] T005 [P] [US1] (FR-002, FR-010, SC-002) In `tests/unit/test_153_card_id_model.py`, assert a stub board keyed
      `PROJ-123` satisfies `BoardProvider` and that `get_card_comments` accepts that id
      unchanged — nothing parses it, casts it, or rejects it.
- [x] T006 [US1] (FR-007, SC-001) In `tests/unit/test_153_card_id_model.py`, drive the **real**
      `route_issue_comments` node with a stub board and a GitHub service asserted unused for the
      comment read. This is the load-bearing test for SC-001; a version that calls
      `fetch_new_issue_comments` directly would not prove the node was reachable.
- [x] T007 [US1] (SC-002) In `tests/unit/test_153_card_id_model.py`, assert a card with **no**
      `issue_number` still routes comments — the regression test for the gate at
      `route_issue_comments.py:36-37` (research R3). Without this, the feature can ship and
      silently do nothing on a non-GitHub board.

### Implementation

- [x] T008 [US1] (FR-001) In `src/coordinare/services/board_provider.py`, add `get_card_comments(card_id,
      since_id=None)` to the `BoardProvider` protocol, documenting the id as opaque, the
      watermark, the oldest-first ordering, and that it returns `[]` rather than raising when the
      read fails.
- [x] T009 [US1] (FR-003) In `src/coordinare/services/board_provider.py`, implement `get_card_comments` on
      `GitHubProjectsBoardProvider`: resolve via `issue_number_for_card`, return `[]` when that is
      `None`, otherwise forward to `get_issue_comments`. The translation lives here and nowhere
      above it.
- [x] T010 [US1] (FR-007, FR-009) In `src/coordinare/services/issue_comment_service.py`, change
      `fetch_new_issue_comments` to `(card_id: str, since_id: int | None, board: BoardProvider)`,
      removing the `issue_number` parameter and sourcing comments from the board. Keep the
      existing `except` → `[]` behaviour and the log line exactly as they are.
- [x] T011 [US1] (FR-007) In `src/coordinare/graph/nodes/route_issue_comments.py`, resolve the board with
      `board_of(state)` and **replace the `issue_number` early-return gate with a `card_id`
      gate**. Keep `issue_number` only where it is passed as event/log metadata (research R6).
- [x] T012 [US1] (FR-009, SC-006) Update `tests/unit/services/test_issue_comment_service.py` for the new
      signature — the six call sites at lines 131, 141, 164, 189, 204, 225. Permitted by SC-006;
      no other existing test file may change.

**Checkpoint**: US1 is independently deliverable — a non-GitHub board routes comments.

---

## Phase 4: User Story 2 — the translation costs nothing (P2)

**Goal**: closing the gap spends no additional GitHub API budget.

**Independent test**: route comments for a polled card and count what the code host received.

### Tests (write first)

- [x] T013 [P] [US2] (FR-004, SC-003) In `tests/unit/test_153_card_id_model.py`, assert the exact recorded call
      sequence is `["poll_board", "get_issue_comments"]` for a polled card. Assert the whole
      sequence, not the absence of one predicted call (research R4).
- [x] T014 [P] [US2] (FR-005) In `tests/unit/test_153_card_id_model.py`, assert the fallback fires for an
      unpolled card, and that a second read of the same card does **not** fire it again — the
      remembering, not just the resolving.
- [x] T015 [P] [US2] (FR-006) In `tests/unit/test_153_card_id_model.py`, assert an unresolvable card id
      yields `[]` and raises nothing, matching today's quiet no-op.
- [x] T016 [P] [US2] (FR-004) In `tests/unit/test_153_card_id_model.py`, assert two `GitHubService`
      instances do not share a map — two symphonies on different repositories can hold the same
      issue number for different cards, so a shared map would be a correctness bug (research R5).

### Implementation

- [x] T017 [US2] (FR-004, FR-005) Confirm T002/T003 satisfy T013-T016 with no further change; if the fallback is
      not remembered, fix it in `src/coordinare/services/github.py`. No adapter-level cache
      (research R1 — it would be dead state, since `board_of` builds a new adapter per call).

**Checkpoint**: both stories complete.

---

## Phase 5: Correct the record (FR-011)

- [x] T018 (FR-011, SC-007) In `src/coordinare/services/board_provider.py`, replace the module docstring paragraph
      beginning "Reading a card's comments is not here either" with what is now true. Do not
      delete it silently — spec 149's merged text describes this gap.
- [x] T019 (FR-011, SC-004) In `tests/unit/test_149_board_provider.py`, add `get_card_comments` to
      `EXPECTED_SURFACE` so the surface stays **pinned at five**, and rewrite
      `TestTheCommentGapIsStatedRatherThanHidden` to assert the operation is present and the
      docstring no longer claims a gap. Rewrite, do not delete: an unpinned surface is how the
      protocol grows without anyone deciding to grow it.
- [x] T020 (FR-011, SC-007) Update `specs/149-board-provider/spec.md`'s Key Entities note, which still says
      reading card comments is deliberately absent, to point at this spec.

## Phase 6: Verification and review

- [x] T021 `.venv/bin/ruff check src tests` and `.venv/bin/mypy` on the changed modules.
- [x] T022 Full suite the way CI runs it: `.venv/bin/pytest tests/ -q`.
- [x] T023 (SC-006) Confirm SC-006 with `git diff --stat main...HEAD -- tests/`. Four files, not
      the three this list first named: `tests/unit/graph/nodes/test_route_issue_comments.py` is
      also a comment-routing test, which SC-006 permits — the task list was narrower than the
      spec, and the spec is right.
- [x] T024 (FR-008, SC-005) Confirm SC-005: `git diff main...HEAD -- src/coordinare/graph/nodes/notify.py` is
      empty. Also grep the comment path for any remaining `issue_number` early-return gate
      (plan Risks) rather than trusting the one known site.
- [x] T025 Acceptance re-read against SC-001..SC-007; record anything not literally met.
- [x] T026 Commit code and spec together, referencing #232.
- [x] T027 Adversarial `Workflow` review over the full branch diff before merge. Lenses: the map
      never populating, a missed early-return gate, protocol/test honesty, behaviour drift in the
      re-signatured service, and whether the spec-149 tests were weakened rather than updated.

---

## Dependencies

```
Phase 1 (T001)
   └─> Phase 2 (T002, T003)          # both stories need the translation
          ├─> Phase 3 US1 (T004-T012)   # MVP
          └─> Phase 4 US2 (T013-T017)   # independent of US1's node work
                 └─> Phase 5 (T018-T020)
                        └─> Phase 6 (T021-T027)
```

US1 and US2 both depend on Phase 2 but not on each other: US1 is the seam and the node, US2 is
the cost of the translation.

## Parallel opportunities

- T004, T005 together (different test classes, no shared state).
- T013-T016 together — all four are independent assertions over the same fake.
- T008 and T010 must be sequential: T010 calls what T008 declares.

## Implementation strategy

MVP is **Phase 1 + Phase 2 + Phase 3 (US1)**: at that point a non-GitHub board routes comments
and the seam is closed. Phase 4 makes it deployable under a rate limit; Phase 5 stops the
repository from asserting a limitation that no longer exists.

## Phase 7: Review remediation (adversarial `Workflow`, 3 confirmed / 5 refuted)

- [x] T028 The exception path in `issue_number_for_card` did not remember its failure, so a
      card whose lookup threw retried on every cycle — the same defect as the empty-result
      path, in the branch I had not fixed, and worst exactly when the API is already
      unhappy. Now remembered; a later poll heals it.
- [x] T029 Added the missing exception-path tests (the review's third finding): a throwing
      lookup happens once, and a poll can still resolve the card afterwards.
- [x] T030 The poll/fallback asymmetry around `0` was reported as an inconsistency. Kept, and
      the reasoning moved into the docstring: a poll's `0` means "could not read a number
      here", including from a partial response, so letting it overwrite a good entry would
      turn a blip into a card coordinare believes unreadable. A lookup aimed at one card is
      evidence about that card; a poll's zero is not.
- [x] T027 Adversarial `Workflow` review over the full branch diff.
