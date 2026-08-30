# Tasks: BoardProvider Protocol

**Issue**: #203 | **Branch**: `149-board-provider-protocol`

**Guardrails**: no existing test may be edited — if one needs to change, the extraction is wrong;
do not abstract the code host; no push or PR without approval.

## Phase 1: The protocol

- [x] T001 (FR-001, FR-002, FR-006) Define `BoardProvider` in `src/coordinare/services/board_provider.py` from the nine operations coordinare calls: poll_board, move_card, get_issue_details, add_comment, get_issue_comments, check_issue_state, list_open_issues, add_labels, ensure_labels_exist.
- [x] T002 Model the move result so a legitimate refusal (a Jira workflow forbidding a transition) is an outcome, not an exception representing a fault (FR-005).
- [x] T003 Make unsupported operations declarable, so no caller branches on provider type (FR-007).
- [x] T003b [P] (FR-006) Test the protocol carries card ids opaquely — nothing in it assumes a card id indexes the code host, which is true on GitHub Projects and false on Jira.
- [x] T004 [P] (SC-002) Test the protocol's surface is exactly the called set — no method that no call site uses (SC-002).
- [x] T005 [P] Test on paper against Trello (lists are lanes, moves always succeed, no labels) and Jira (restricted transitions), asserting neither needs a special case (SC-006).

## Phase 2: The GitHub provider

- [x] T006 (FR-003) `GitHubProjectsBoardProvider` delegating to the existing `GitHubService`. Delegation, not reimplementation: the behaviour must be the same code.
- [x] T007 [P] Test it satisfies the protocol and that statuses cross the boundary as canonical `CardStatus`, never a native lane name (FR-009).

## Phase 3: Classify every call site by the ID it passes (do this FIRST)

- [x] T007b Enumerate all 26 external call sites and label each **card** or **code-host** by the
  value passed, not the method name. `add_comment` is polymorphic — `notify.py` passes a
  `pr_node_id` — so a name-driven migration would route PR comments through the board provider.
  That works today (both are GitHub) and breaks silently when a second provider exists.

## Phase 3b: Genuinely board-only call sites (low risk)

- [x] T008 Retype the callers T007b confirms are board-only. NOTE: `notify.py` was on this list from the name-based survey and does NOT belong — it comments on pull requests. Run the full suite after this phase.

## Phase 4: Mixed call sites (9 files — the real work)

- [x] T009 Thread a board collaborator through `check_board.py` alongside the code host. Do this one FIRST: it is the core loop and the hardest, so a problem with the approach shows up before eight more files are changed.
- [x] T010 Then, one file at a time with the suite run after each: monitor_performer, monitor_pr, dispatch_performer, merge_pr, assess_card, advocate, daemon, __main__.
- [x] T011 [P] (FR-004, SC-004) Test no provider-type conditional or isinstance check exists in the daemon or graph (SC-004).
- [x] T012 [P] Test no code-host operation leaked into the protocol (SC-005, FR-008).

## Phase 5: Proving the seam

- [x] T013 A stub `BoardProvider` drives a full cycle with **no coordinare source change** (SC-003). This is the criterion that distinguishes a real seam from a protocol that merely exists.

## Phase 6: Verification

- [x] T014 (FR-003, SC-001) Full suite the way CI runs it (bare `pytest`), plus `make lint`. **No existing test edited** — verify with `git diff --stat` over `tests/`.
- [x] T015 Acceptance re-read against SC-001..SC-006; record anything not literally met.
- [x] T016 Commit code and spec together, `Closes #203` only if the issue's phase 1 is its whole scope; otherwise reference it.
- [ ] T017 Adversarial `Workflow` review over the full branch diff before merge. Lenses: behaviour drift in the refactor, protocol bloat, mixed-site threading errors, test honesty.

## Dependencies

```
Phase 1 -> Phase 2 -> Phase 3 -> Phase 4 -> Phase 5 -> Phase 6
                                    ^
                        T009 (check_board) before T010
```

## MVP

Phases 1-3 produce a protocol and prove it on the easy half. The feature is not real until Phase 4,
because the core loop is a mixed site.

## Phase 6: Review remediation (adversarial `Workflow`, 6 confirmed / 5 refuted)

- [x] T018 `cancel.py` resolved the board through `board_of(state)` instead of constructing
      `GitHubProjectsBoardProvider(github)`, which ignored any configured provider.
- [x] T019 Dropped `get_card_comments` from the protocol: nothing called it (SC-002) and its
      adapter took an issue number where the protocol promised a card id. Gap documented and
      filed as #232 — the fix is the card id model, not a wider protocol.
- [x] T020 Added `move_card_or_warn` and moved all 26 call sites onto it, so a refused
      `MoveOutcome` is reported rather than discarded. Tests cover refusal, acceptance, and
      the missing-board raise that preserves the old behaviour.
- [x] T021 Tightened the AST leak test to require the exact `board_provider` receiver
      (a substring test would have accepted `board_provider_github`), and added a second AST
      test forbidding bare `board_provider.move_card`.
- [x] T022 Stripped ~1,100 lines of `ruff format` churn the migration had introduced into
      files main never had formatted, proving each de-churned file normalises byte-identical
      to the tested version. Source diff: 1,220 lines → 111.
- [x] T017 Adversarial `Workflow` review over the full branch diff.
