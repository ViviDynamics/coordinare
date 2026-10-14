# Issue pipeline budget (#321)

`max_concurrent_cards` limits admitted issue pipelines in the effective symphony
configuration. Performer-role slots impose additional resource limits. Remembering
an issue during startup reconciliation no longer gives it automatic permission to
dispatch work outside the pipeline budget.

The scheduler preserves admitted issues across stage handoffs. New eligible issues
enter in retained session order. Done, canceled and blocked issues release their
reservation; an unblocked issue rejoins the queue. Reservations are persisted per
session, with old snapshots defaulting to not admitted. Existing live performers are
always retained: if their count exceeds a reduced budget, they drain without another
issue being admitted. No running job is interrupted to enforce it.

**Changed by #179 (2026-10-13).** An issue waiting on PR review no longer keeps its
reservation. The original rule held it "across PR review", so with a limit of one a
single unreviewed PR stopped the whole symphony. The rule is now: an issue holds a
slot while it has work of ours to do or under way.

- An issue parked in review (`monitoring_pr`, no live performer, no live side writer)
  releases its reservation. It is still ticked every cycle so its merge evaluation runs.
- When it needs a performer again (review feedback, a failing check, a rebase) it is
  admitted ahead of issues that have not started, once a slot is free. It never
  displaces an issue that already holds a slot.
- `max_cards_in_review` (default 3, `null` for none) caps how many issues may be parked
  in review. When the cap is reached, issues that have not started are not admitted and
  their skip reason is `review_capacity`. Issues already in flight continue and park
  when they finish, so the number in review can pass the cap by up to
  `max_concurrent_cards - 1`. The effective cap is never below `max_concurrent_cards`.

**Changed by #186 (2026-10-13).** "Parked" now means waiting on a **human** review, not
waiting on PR review. The #179 rule released the slot for any wait in `monitoring_pr`,
which included CI checks and anything the daemon would act on next. Only a human wait
releases a slot, so that a card on checks keeps its reservation and is not counted toward
`max_cards_in_review`.

- `monitor_pr` records what the card is waiting on in the session's `review_wait`:
  `human` (no approval yet, an approval on an older commit, a stale approval withdrawn by
  its reviewer, or an outstanding change request), `automation` (a pending or failing
  check, a base-branch check that is red, or comments not yet fully read), or absent when
  not evaluated. Only `human` is parked. Absent holds the slot, so a card that has just
  entered `monitoring_pr`, or came back after a restart, is not released until it has been
  evaluated. The field is cleared whenever the card leaves `monitoring_pr`, and it is not
  persisted: after a restart it is absent until the next evaluation, and the card is still
  ticked in the meantime.
- "Started" means the card has an open PR, or its session holds a finished stage's record
  (the assessment, the blueprint or a stage verdict). A started card that has lost its PR
  fields (blocked then unblocked, or readopted after restart) is returning work: it is
  admitted ahead of new work and is not held by the review cap.
- Same cycle. A parked card learns in its own tick that it needs a performer, after the
  cycle's selection has been made. So the fanout first ticks the cards in `monitoring_pr`
  against a selection of started work only, which leaves a free slot for a returning card
  to claim. The full selection is made after that pass, and the other cards are ticked
  then. A card the selection did not admit is not ticked at all, so it cannot dispatch.
  No running performer is interrupted, and the number of performers running never exceeds
  `max_concurrent_cards`.
- Liveness has one definition, `has_live_work`: a live performer or agent with a session,
  or a live side writer. Pickup counting in `check_board` and the budget both use it.
**Bounded holds (#186 follow-up).** A card may keep its slot in `monitoring_pr` for
`review_slot_hold_seconds` (default 900) while it waits on checks or the daemon, and the
same for a merge that keeps failing in `merging`. After that it is recorded as a wait on a
human, and `monitor_pr.slot_hold_expired` is logged once with the reason. One clock runs
per hold episode, on the monotonic clock. It starts when the card enters a non-human hold,
and it is not restarted by a new head or a new reason, which are logged but do not reset
it. It ends when the card reaches a human wait, dispatches a performer, or leaves review or
merge. An expired hold stays a human wait until then. A card with a live side writer or a
live performer is never released. A released merge is still ticked every cycle, so it is
retried. A red base branch, a comment poll that cannot complete (no PR number), and a
non-transient `monitor_pr` error are human waits at once. A declined assessment is not
"started": a card that comes back to To Do is new work again.

The clock is not persisted. After a daemon restart a card in a non-human hold has no clock,
so it is timed again from its first sighting in the next cycle, and it is evaluated in the
first pass like any other card in review.

- In the symphony override editor, "No cap (unlimited)" saves `null` for
  `max_cards_in_review`. An empty input removes the override and inherits the global default.

Queued sessions remain tracked and show `Queued — issue limit reached` in performer
skip diagnostics, or `Queued: too many cards waiting on review` when the review cap
is what holds them. Blocked-card polling remains bounded and independent; the dispatch
boundary checks the budget again so recovery cannot bypass it. Empty-session legacy
single-card callers continue to operate.

For initial website QA, stop the daemon before resetting board status and preserve
its state/branches/PRs. Validate the effective symphony limit, then check the first
post-restart dispatches: restoring six cards with limit two must not dispatch six
pipelines. Lowering to one should keep one issue selected across all its stages, until it parks
in review; the next issue then starts.

Retained sessions reconciled to TODO/idle are rehydrated from the board and made
dispatchable once their dependencies are satisfied. Their performer stage and
comment/review watermarks are preserved. Tracking these queued sessions does not
give them permission to bypass the issue budget.
