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
