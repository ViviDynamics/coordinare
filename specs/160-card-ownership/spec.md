# Feature Specification: Coordinare works its own cards, and only its own

**Feature Branch**: `160-card-ownership`
**Created**: 2026-09-03
**Status**: Draft
**Input**: Coordinare should only pick up the issues assigned to it, with an optional per-symphony
setting to opt into picking up unassigned issues too.

## Context

Spec 050 added `assignee_filter`: name a GitHub login and coordinare dispatches only TODO cards
carrying that assignee. It works, and it covers one of the four ways coordinare takes a card.

`check_board` adopts a card — creates a session for it and puts a performer on it — from four
places:

| Column | What adoption means | Gated by 050 |
| --- | --- | --- |
| TODO | pick up and dispatch | yes |
| IN_PROGRESS | re-adopt and resume, deriving a stage from branch artifacts | **no** |
| IN_REVIEW | re-adopt as a passive PR-monitoring session | **no** |
| BLOCKED | read the issue, post reminders, resume on a new comment | **no** |

The three unguarded ones matter because they are how a *human* hands coordinare a card by hand.
Someone drags their own work-in-progress into In progress and coordinare readopts it, derives a
resume stage, and dispatches an implementer at a branch that is not its own. On a shared board
that is not a filter with a gap in it; it is coordinare working other people's cards.

The second half is the opposite need. On a board where nobody assigns anything, a filter is all
cost: every card is unassigned, so a configured filter stops the daemon dead. Today the only way
to run such a board is to leave the filter off entirely, which also admits cards explicitly
assigned to someone else.

## User Scenarios

### US1 — A shared board (P1)

A board carries coordinare's cards and two engineers' cards. `assignee_filter` names coordinare's
login. Coordinare dispatches its own TODO cards, resumes its own In progress and In review cards,
and reminds on its own Blocked cards. It touches nothing else, in any column.

### US2 — A board nobody assigns on (P2)

Every card on the symphony's board is coordinare's, and nobody bothers assigning them. The
symphony sets `include_unassigned: true` alongside the filter. Coordinare takes unassigned cards
as well as its own — and still leaves a card explicitly assigned to a person alone, which is what
makes the setting different from switching the filter off.

### US3 — A card is unassigned mid-flight (P2)

Someone clears the assignee on a card coordinare has a performer working. The performer is not
killed, the branch is not abandoned, and the card is not read as having left the board. Ownership
decides what coordinare *starts*, not what it abandons.

## Requirements

- **FR-001**: A single ownership decision, consulted by every path in `check_board` that adopts a
  card with no existing session: TODO pickup, IN_PROGRESS re-adoption, IN_REVIEW re-adoption, and
  BLOCKED handling.
- **FR-002**: With no `assignee_filter` configured, every card is eligible — the behaviour of
  every deployment that has never set one, unchanged.
- **FR-003**: Eligibility is the **union of two opt-ins**: `assignee_filter` admits cards
  assigned to the login it names, `include_unassigned` (default `false`) admits cards carrying
  no assignee. A card assigned to somebody else is in neither. With neither set, every card is
  eligible. `include_unassigned` set alone is a complete policy — the eligible set is
  `{assigned to login} | {unassigned}` and the first half is empty, leaving "unassigned only".
  That is the shape a deployment authenticating as a GitHub App requires, since an App cannot
  be assigned to an issue and so has no login to name; assigning a card to a human is then how
  work is taken off coordinare. An earlier revision made the flag a modifier that did nothing
  without a filter, expressible only by inventing a dummy login.
- **FR-004**: `include_unassigned` is a `ProjectConfiguration` field, so a symphony sets it in
  `overrides` beside `assignee_filter`. The pair travels together rather than one being global
  and the other per-symphony.
- **FR-005**: A card with an existing session is never re-checked. Ownership gates session
  *creation* only.
- **FR-006**: The column lists themselves are never filtered. The disappeared-card check asks
  whether the active card is still anywhere on the board; asked of a filtered list it would read
  an unassignment as a deletion and cancel live work.
- **FR-007**: 129's blocked-card recovery, which *moves* cards on the board, sees only owned
  cards.
- **FR-008**: A card the last poll reported no assignees for is treated as unassigned, not as a
  match. An absent entry and an empty list are the same fact.
- **FR-009**: The filter is matched case-insensitively, both sides lowercased.
- **FR-011**: When a filter is active and **not one card on the board is coordinare's**, coordinare
  warns rather than going quiet. Two narrower conditions were tried and both failed against
  measurement. Keyed off the `item_assignees` map being empty, it could never fire at all: the
  GitHub poller seeds `item_assignees[item_id] = []` for every item before it reads the content,
  so the map is empty only when the board is. Keyed off no card carrying *any* assignee, it misses
  the case an operator actually hits — measured on `ViviDynamics/website`, 73 cards with 8 carrying
  an assignee and none of them coordinare's, so setting a filter takes the board from 73 workable
  cards to zero in silence. The event carries `cards_on_board`, `cards_with_an_assignee` and
  `assignee_data_present`, which together separate the three shapes an operator must tell apart:
  nobody assigned anywhere (answered by `include_unassigned`), other people assigned but not
  coordinare (assign cards to it, or fix the login), and no assignee data at all (a provider or
  config problem). It does not fire while the filter is finding work, nor when `include_unassigned`
  has already made the board workable.
- **FR-012**: That warning fires on *entering* the state, not while it persists, and re-arms when
  the condition clears. `check_board` runs once per poll while no session is adopted, which is
  precisely the situation being warned about, so an unlatched warning is 120 identical lines an
  hour at the default interval. The latch is also what makes FR-011's broader condition
  affordable: once per episode on a shared board where the filter is legitimately excluding
  everything is a signal, every cycle would be noise.
- **FR-010**: The dashboard names the policy in force, in both the idle panel and the
  active-performers panel. The string is computed from the same `ownership_policy()` the gate
  consults rather than re-derived from the config fields in the browser, so the hint cannot
  disagree with the gate it describes. It has three shapes, `coordinare-bot + unassigned`,
  `coordinare-bot` and `unassigned`, and the empty hint is a claim too: no policy is in force and
  every card on the board is coordinare's. Gating the hint on `assignee_filter` was correct only
  while `include_unassigned` was a modifier that did nothing alone. Under FR-003's union it left
  the one configuration an App deployment can use rendering as no policy at all, on a board where
  the gate was excluding cards every cycle.

### Superseded

Spec 050's FR-003 specified a `check_board.assignee_filtered` log event. The decision now applies
in four places rather than one, so the event is `card_ownership.filtered`, carrying a `context`
field (`todo` / `blocked` / `blocked_recovery`) alongside the existing `skipped` count and filter
value.

## Success Criteria

- **SC-001**: With a filter set, a card assigned to someone else in each of TODO, IN_PROGRESS,
  IN_REVIEW and BLOCKED is left untouched — no session, no dispatch, no issue read, no reminder.
- **SC-002**: With `include_unassigned: true`, an unassigned TODO card dispatches and a card
  assigned to another login still does not.
- **SC-003**: Clearing the assignee on a card with a live performer relays no cancellation and
  moves no card.
- **SC-004**: With no filter configured, every column is adopted exactly as before.
- **SC-005**: A board on which no card is coordinare's, with a filter set, produces a warning naming
  the card count and how many carry an assignee — not silence, and not on every poll.

## Out of Scope

- Coordinare assigning cards to itself. It never has, and a card it assigned to itself would make
  the filter self-fulfilling rather than a statement of intent.
- Deriving "itself" from the GitHub App's identity. Verified against the live repo rather than
  assumed: `vivi-coordinare[bot]` exists (a real `Bot` user, id 273876795) but
  `GET /repos/ViviDynamics/website/assignees/vivi-coordinare[bot]` returns 404 where `Jason733i`
  returns 204, it is absent from `suggestedActors(capabilities:[CAN_BE_ASSIGNED])`, and
  `addAssigneesToAssignable` against a real issue answers `FORBIDDEN: Could not assign agent:
  vivi-coordinare[bot] cannot be assigned to issues or pull requests`. GitHub does allow bot
  assignees for agents it enables itself (the Copilot coding agent), but not for an arbitrary
  App, so the login has to be one an operator names — a machine user, or nobody at all with
  `include_unassigned`.
- A per-symphony editor for either setting in the config UI. The UI's override editor handles
  text and numbers only, and wiring the global half alone would let an operator flip a value that
  a symphony override silently overrules.
