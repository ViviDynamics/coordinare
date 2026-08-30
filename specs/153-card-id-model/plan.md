# Implementation Plan: One card id model

**Branch**: `153-card-id-model` | **Date**: 2026-08-30 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/153-card-id-model/spec.md`
**Issue**: #232 (follow-up to spec 149 / #203)

## Summary

Restore `get_card_comments` to the `BoardProvider` protocol, addressed by the same opaque card id
as every other board operation, so a non-GitHub board can route card comments — the last gap in
the seam spec 149 built.

The approach turns on one fact: `GitHubService.poll_board()` already computes the card-id →
issue-number pairing every cycle. Remembering it makes the translation free, so the GitHub
adapter can honour a card-id contract without an extra API call. The map lives on
`GitHubService`, **not** on the adapter, because `board_of()` and `daemon.py` construct fresh
adapters constantly and instance state would never survive (research R1).

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing only — `BoardProvider`/`GitHubProjectsBoardProvider`/`board_of`
(spec 149), `GitHubService.poll_board`/`get_issue_details`/`get_issue_comments`, the
`route_issue_comments` graph node, `issue_comment_service`, structlog. **No new dependency.**
**Storage**: N/A. No persisted state, no `state_store.py` schema change. The identifier map is
in-memory, per service instance, rebuilt by the first poll after a restart.
**Testing**: pytest, `tests/unit/`, following `tests/unit/test_149_board_provider.py`
**Target Platform**: Linux/macOS single-host single-process daemon
**Project Type**: single
**Performance Goals**: zero additional GitHub API calls in the common path (SC-003)
**Constraints**: no observable behaviour change — same comments, same underlying call, same
watermark semantics
**Scale/Scope**: 5 source files, 1 new test file, 2 existing test files updated

## Constitution Check

| Principle | Assessment |
|---|---|
| I. Code Quality First | Passes. The change removes a duplicated concept (two ids for one card) rather than adding one. Translation is confined to the adapter and service — the two places allowed to know about GitHub. Type hints on all new signatures. |
| II. Testing Discipline (NON-NEGOTIABLE) | Tests written before implementation. Deterministic: a hand-written fake, no `AsyncMock` auto-attributes (research R4), no network, no sleeps. Coverage does not decrease — the new path is covered and no code is deleted untested. |
| III. User Experience Consistency | Passes, and improves it: an operator on a non-GitHub board currently gets silent non-routing (research R3's gate leak). Failure stays a logged no-op rather than a stalled cycle (FR-006). |
| Minimal dependencies | Passes — none added. |

No violations. Complexity Tracking section omitted as unused.

## Project Structure

### Documentation (this feature)

```
specs/153-card-id-model/
├── spec.md
├── plan.md              # this file
├── research.md          # R1-R7, the decisions and what was rejected
├── data-model.md
├── quickstart.md
├── checklists/requirements.md
└── contracts/board-provider.md
```

### Source Code (repository root)

```
src/coordinare/
├── services/
│   ├── board_provider.py          # + get_card_comments on protocol and adapter;
│   │                              #   docstring gap paragraph replaced (FR-011)
│   ├── github.py                  # + remembers issue_numbers in poll_board;
│   │                              #   + issue_number_for_card() with fallback
│   └── issue_comment_service.py   # fetch_new_issue_comments takes (card_id, since_id, board)
└── graph/nodes/
    ├── route_issue_comments.py    # resolves the board; gate on card_id, not issue_number
    └── notify.py                  # UNCHANGED — PR comments are code-host work

tests/unit/
├── test_153_card_id_model.py            # new
├── test_149_board_provider.py           # surface pin + now-false gap assertions
└── services/test_issue_comment_service.py  # changed signature
```

## Phases

**Phase 1 — Tests first (Constitution II).** Write `test_153_card_id_model.py` covering: the
protocol surface grew by exactly one and stays opaque; a `PROJ-123` card routes comments through
the real node; the code host receives exactly `["poll_board", "get_issue_comments"]`; the
fallback resolves and is remembered; an unresolvable id yields `[]` and does not raise; two
services do not share a map.

**Phase 2 — The GitHub side.** `poll_board` remembers the pairing; `issue_number_for_card`
resolves from memory, then via issue details, remembering the result.

**Phase 3 — The seam.** Add `get_card_comments` to the protocol and the adapter.

**Phase 4 — The callers.** Re-signature `fetch_new_issue_comments`; migrate
`route_issue_comments` to `board_of(state)` and replace the `issue_number` early-return gate with
a `card_id` gate (research R3 — without this the feature silently does nothing on a non-GitHub
board).

**Phase 5 — Correct the record (FR-011).** Update the spec-149 docstring paragraph and its two
test classes to assert what is now true, rather than deleting the assertions.

**Phase 6 — Verify and review.** Full suite, lint, mypy on changed modules, `git diff --stat`
over `tests/` to confirm SC-006, then the mandatory adversarial `Workflow` review over the full
branch diff before merge.

## Risks

- **The map never populates.** The failure this design exists to avoid, and it would be invisible
  to a naive test that reuses one adapter. Mitigated by R1's placement and by a test asserting
  the call sequence rather than the result alone.
- **A missed early-return gate.** `route_issue_comments.py:37` is one; a second elsewhere would
  reproduce the same silent no-op. Phase 4 greps for `issue_number` across the comment path
  rather than trusting the known site.
- **Correcting spec 149's tests by weakening them.** Explicitly called out in research R7: the
  surface must stay pinned, asserting five operations instead of four.
