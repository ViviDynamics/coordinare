# Issue pipeline budget (#321)

`max_concurrent_cards` limits admitted issue pipelines in the effective symphony
configuration. Performer-role slots impose additional resource limits. Remembering
an issue during startup reconciliation no longer gives it automatic permission to
dispatch work outside the pipeline budget.

The scheduler preserves admitted issues across stage handoffs and PR review. New
eligible issues enter in retained session order. Done, canceled and blocked issues
release their reservation; an unblocked issue rejoins the queue. Reservations are
persisted per session, with old snapshots defaulting to not admitted. Existing live
performers are always retained: if their count exceeds a reduced budget, they drain
without another issue being admitted. No running job is interrupted to enforce it.

Queued sessions remain tracked and show `Queued — issue limit reached` in performer
skip diagnostics. Blocked-card polling remains bounded and independent; the dispatch
boundary checks the budget again so recovery cannot bypass it. Empty-session legacy
single-card callers continue to operate.

For initial website QA, stop the daemon before resetting board status and preserve
its state/branches/PRs. Validate the effective symphony limit, then check the first
post-restart dispatches: restoring six cards with limit two must not dispatch six
pipelines. Lowering to one should keep one issue selected across all its stages.

Retained sessions reconciled to TODO/idle are rehydrated from the board and made
dispatchable once their dependencies are satisfied. Their performer stage and
comment/review watermarks are preserved. Tracking these queued sessions does not
give them permission to bypass the issue budget.
