# Research: One card id model (spec 153, issue #232)

All paths verified against `main` at 35dd182 on 2026-08-30.

## R1 — Where the identifier map lives

**Decision**: on `GitHubService`, remembered by `poll_board()`. Not on the adapter.

**Rationale**: the obvious design — give `GitHubProjectsBoardProvider` a `self._issue_numbers`
dict filled in by its own `poll_board` — cannot work here, and the reason is worth stating
because it is invisible until you look at the construction sites.

`board_of()` returns `GitHubProjectsBoardProvider(github)` — a **brand new adapter on every
call** (`services/board_provider.py:190`). Call sites invoke `board_of(state)` once per node
invocation, so the adapter is rebuilt many times per cycle. `daemon.py` compounds it, building
fresh adapters on every per-symphony swap (`daemon.py:1932` and `:1944`). An adapter-instance
map would therefore be empty essentially every time it was read, the fallback would fire on
every single comment fetch, and FR-004 — the requirement that costs no extra API call — would
be violated in exactly the common case it exists to protect. The design would still pass a unit
test that constructed one adapter and used it twice.

Putting the map on the service is also the more honest placement. GitHub is the host with two
identifiers for one card; that is GitHub's problem, and `GitHubService` is the object whose job
is knowing about GitHub. It is already the thing that fetches the pairing: `poll_board()` builds
`issue_numbers: dict[str, int]` at `services/github.py:861-897` and returns it at `:950`.
Remembering what it just computed adds no call and no new concept.

It keeps the adapter a pure forwarder, which was spec 149's stated warrant for claiming "no
behaviour change" structurally rather than by testing for it.

**Alternatives considered**:
- *Memoise adapters per service* (a `WeakKeyDictionary` in `board_of`). Would preserve
  instance state, but introduces a cache with lifetime questions to solve a problem that
  disappears entirely if the map lives where the data already comes from.
- *A module-level cache keyed by card id.* Global mutable state shared across symphonies —
  see R5 for why that is actively wrong here.
- *Pass the issue number down from the card dict.* This is the two-id model with extra steps,
  and it is what the spec exists to remove.

## R2 — The fallback, and what an unresolvable id does

**Decision**: on a miss, resolve via `get_issue_details(card_id)["number"]` and remember the
result in the same map. An id that still cannot be resolved yields no comments.

**Rationale**: the map is populated by polling, so there are two real gaps — a card read before
the first poll of a freshly started process, and a card that has left the polled set. The
GraphQL query at `services/github.py:161-177` selects both `id` and `number`, so
`get_issue_details` already returns what is needed; this is a call coordinare makes elsewhere,
not a new capability.

Remembering the fallback result matters: without it, a card outside the polled set would pay the
extra call on *every* cycle rather than once.

"Yields no comments" was checked against today's behaviour rather than assumed.
`fetch_new_issue_comments` returns `[]` when `issue_number` is falsy
(`issue_comment_service.py:190-191`) and returns `[]` on any exception from the fetch
(`:194-201`). `GitHubService.get_issue_comments` itself also swallows request failures and
returns `[]` (`github.py:1078-1080`). So a card coordinare cannot resolve is already a quiet
no-op, and FR-006 preserves that rather than introducing it. Raising here would turn a board
hiccup into a stalled cycle.

## R3 — The `fetch_new_issue_comments` signature

**Decision**: `fetch_new_issue_comments(card_id: str, since_id: int | None, board: BoardProvider)`.
The `issue_number` parameter is removed.

**Rationale**: the function already receives `card_id` and uses it only for logging, passing
`issue_number` onward as the real key. Keeping both would preserve the two-id model in the one
signature the spec is about.

Verified there is no other production caller: the only one is
`graph/nodes/route_issue_comments.py:44`. The other references are the function's own log line
and `tests/unit/services/test_issue_comment_service.py`, which is a comment-routing test file and
therefore inside the set SC-006 permits changing.

**A leak this exposed**: `route_issue_comments.py:36-37` reads
`issue_number = int(card.get("issue_number") or 0)` and returns early if it is falsy. That gate
is a GitHub assumption sitting in a graph node — a Jira card has no issue number, so it would
return early forever and route nothing, no matter how correct the provider beneath it was. The
gate must become a `card_id` gate. This was not in the original scope list and is the kind of
thing that would have made the feature silently not work.

## R4 — Asserting "exactly one request of the code host" (SC-003)

**Decision**: a fake service that records **every** method call by name, asserted against the
whole recorded list — not `assert mock.get_issue_details.call_count == 0`.

**Rationale**: asserting the absence of one specific extra call only catches the extra call you
predicted. The failure mode SC-003 guards against is *any* additional round trip, including one
introduced later by a well-meaning change. Recording all calls and asserting the exact sequence
`["poll_board", "get_issue_comments"]` fails on anything unexpected, and reads as the claim it
is making.

`unittest.mock.AsyncMock` is unsuitable here: it auto-creates attributes, so a typo'd assertion
passes, and it would record calls coordinare never made. A small hand-written fake is used
instead, following the existing `_FakeGitHub` pattern in `tests/unit/test_149_board_provider.py`.

## R5 — Per-symphony isolation

**Decision**: correct by construction, because the map lives on the service.

**Rationale**: `daemon.py:1932` swaps in a provider wrapping that symphony's own
`GitHubService`. Since each symphony has its own service, each gets its own map, and no card
identifier can cross between symphonies. Two symphonies on different repositories can hold the
same issue *number* for different cards, so a shared map would be a genuine correctness bug, not
merely untidy — this is the concrete reason a module-level cache (R1's third alternative) was
rejected.

Coordinare is single-host single-process, so no locking is required for the dict itself.

## R6 — What crosses the protocol boundary

**Decision**: the existing normalised comment shape,
`{"id": int, "author": str, "body": str, "created_at": str}`. No GitHub shape leaks.

**Rationale**: checked rather than assumed. `GitHubService.get_issue_comments` does not return
raw GitHub payloads — it normalises each comment at `github.py:1063-1068`, flattening
GitHub's `user.login` to `author` and keeping four plain fields. So the shape a provider must
produce is already host-neutral, and a Jira provider supplies the same four keys with no
pretence. FR-009's "same shape as today" and FR-002's "opaque id" are satisfied by the same
decision.

One field does keep a GitHub name: `IssueCommentEvent.issue_number`
(`issue_comment_service.py:65`). It is *constructed* by this code path, not returned by the
provider, and no consumer reads it — the dashboard's `sess.issue_number` hits session state, a
different object. It is left in place as metadata rather than removed, because removing it would
be a data-model change beyond this spec's warrant, and populating it costs nothing where a number
is known. This is stated so the next reader does not mistake it for the two-id model surviving.

## R7 — Correcting the record (FR-011)

**Decision**: update, in the same commit as the behaviour, the `board_provider` module docstring
paragraph beginning "Reading a card's comments is not here either", the `EXPECTED_SURFACE`
constant, and `TestTheCommentGapIsStatedRatherThanHidden` in
`tests/unit/test_149_board_provider.py`.

**Rationale**: those assert a limitation that this spec removes. Left alone, the suite would fail
— which is the good case. The bad case is "fixing" them by deleting the assertions, leaving spec
149's merged text still describing a gap that no longer exists. The tests are rewritten to assert
the *new* truth (the operation is present, the id is opaque, the docstring no longer claims a
gap) so the surface stays pinned rather than unpinned.

## R8 — Node ordering, and the two costs the design actually has

**Found by tracing the graph, not by a failing test.** `builder.py:87-88` wires
`advocate_scan → route_issue_comments → check_board`, and `check_board` is what calls
`poll_board`. So comment routing runs *before* the poll in every cycle.

Two consequences, both accepted, both worth stating rather than leaving for someone to
discover:

1. **Cold start costs one lookup per active card.** On the first cycle of a fresh process
   the map is empty, so the active card resolves through the fallback. From the second
   cycle onward the previous cycle's poll has supplied the pairing. SC-003's "exactly one
   request" is therefore a claim about the steady state, which is where a daemon spends
   essentially all of its life. Paying one lookup once per process is not worth
   reordering the graph for.

2. **A card that can never resolve must be remembered as such.** A draft issue in a GitHub
   project has no issue number — `poll_board` records `0` for it (`github.py:898-899`).
   Without recording the failure, such a card would repeat the details lookup on every
   cycle forever, which is precisely the per-cycle API cost this design exists to avoid,
   reintroduced through the failure path. So the fallback stores `0` as "known
   unresolvable".

   That negative entry must not be permanent: a draft converted to a real issue gains a
   number. It is not, because `_remember_issue_numbers` overwrites any entry when a poll
   supplies a real number, and only filters zeroes *coming from the poll* — a poll that
   does not know a number must not be able to mark a card unresolvable.

**Alternative considered**: reordering the graph so polling precedes comment routing. It
would remove cost 1 and nothing else, at the price of changing cycle semantics that
nothing else asked to change — a much larger blast radius than a single first-cycle
lookup.
