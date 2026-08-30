# Implementation Plan: BoardProvider Protocol

**Branch**: `149-board-provider-protocol` | **Spec**: [spec.md](./spec.md) | **Issue**: [#203](https://github.com/ViviDynamics/coordinare/issues/203)

## Summary

Introduce a `BoardProvider` protocol and move the existing GitHub Projects behaviour behind it,
changing nothing else. Success is measured by the existing suite passing **unmodified**.

## What the survey established

Measured, not assumed:

- `GitHubService`: 48 methods, 36 public, **26 called externally**.
- **Nine** of those are board operations. The rest are code-host.
- **17 files call it. Nine of them mix both concerns on one object**, including `check_board.py`,
  which is the core loop.

That last number is the plan's central fact.

## The decision it forces

| | Shape | Consequence |
|---|---|---|
| A | Mixed call sites take **two** collaborators: a board and a code host | Larger diff (9 files), but the seam is real |
| B | Type only the 8 board-only sites as `BoardProvider` | Small diff, but a **half-seam**: `check_board` still binds to GitHub, so a Jira provider could never drive a cycle |

**Take A.** B satisfies the protocol's existence and fails its purpose — SC-003 ("a stub provider
drives a full cycle with no coordinare source change") is unachievable under it, because the core
loop is one of the mixed files. Shipping B would mean claiming a seam that does not separate
anything, and the Jira work would then have to redo it.

## Technical Context

**Language**: Python 3.14. **Dependencies**: none added.

**Key structural point**: `GitHubService` already *structurally* satisfies most of the protocol —
`poll_board`, `move_card`, `get_issue_details` and the rest exist with the right shapes. So the
GitHub provider is largely a naming and typing exercise, not a rewrite. The work is in the call
sites, not the implementation.

**Testing**: the existing suite is the specification. Its passing unmodified is the acceptance
evidence, so no existing test may be edited — an edited test is an admission the behaviour moved.

## Constitution Check

| Principle | Assessment |
|---|---|
| I. Code Quality | A small protocol taken from call sites, not from the service's surface. Prefer duplication over interface bloat. |
| II. Testing | Unusual here: the *existing* suite is the test. New tests cover the protocol's shape and the stub-provider substitution. |
| III. UX Consistency | No operator-visible change at all. That is the point. |

**Gate: PASS.**

## Phases

1. **Protocol** — define `BoardProvider` from the nine call-site operations, including a move
   outcome that can express legitimate refusal.
2. **Provider** — `GitHubProjectsBoardProvider`, delegating to the existing service.
3. **Board-only sites (8)** — retype to the protocol. Low risk, no signature changes.
4. **Mixed sites (9)** — thread a board collaborator alongside the code host. The real work; take
   it one file at a time, running the suite after each.
5. **Substitution test** — a stub provider drives a cycle, proving the seam separates.
6. **Verify** — full suite unmodified, lint, scope check, commit.

## Correction found during Phase 3

The board-only / mixed split above was derived from **method names**, and that is not
sufficient. `add_comment` is polymorphic: it comments on whatever GraphQL node id it is given.

```
notify.py:107,244        add_comment(pr_node_id, ...)   -> CODE HOST
advocate.py:379          add_comment(issue_id, ...)     -> board
monitor_performer:1743   add_comment(subject_id, ...)   -> depends on the caller
```

So `notify.py` is not a board-only file at all — it comments on pull requests — and the remaining
work must classify **each call site by the id it passes**, not by the method it calls. The same
question applies to `get_issue_comments`, which is keyed by issue *number* while cards are
addressed by node id elsewhere.

This is why Phase 4 says to do `check_board.py` first: it is the file where both kinds of comment
occur, so getting it right settles the pattern for the rest.

**Consequence for the remaining phases**: before retyping anything, enumerate every call site's
argument and label it card or code-host from the *value*, not the signature. A migration driven by
method names would silently route PR comments through the board provider — which works today
because both are GitHub, and breaks the moment a second provider exists. That failure would be
invisible until Jira, which is exactly the kind of latent breakage this seam is supposed to prevent.

## Risks

- **A refactor that quietly changes behaviour.** Mitigated by the rule that no existing test may
  be edited: if one needs changing, the extraction is wrong and the change stops.
- **Scope creep into the code host.** Nine files will be tempting to tidy. Out of scope, and
  FR-008 says so.
- **Doing this at the end of a long session.** This is a wide, mechanical refactor touching the
  core loop, and mechanical care is exactly what degrades late. Worth starting fresh.
