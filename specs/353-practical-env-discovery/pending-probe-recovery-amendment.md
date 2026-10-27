# Pending probe recovery

The selected approach is generic recovery of an interrupted pending probe, retaining the attempt's evidence and consumed budget. A new logical attempt remains an explicit operator decision. Refunding an interrupted batch or attaching an old report to a new image would erase spend or weaken evidence identity, so neither is part of this change.

## Durable abandoned reservations

An additive, typed `probe_abandonments` field records the complete reserved plan, its digest, a bounded reason, a timestamp and confirmed cleanup. It is separate from `probe_reports` and cannot supply observations or acceptance. Existing version 9 journals without the field read as an empty list; older source protocols retain their existing restart requirements.

The cumulative `probe_batches` counter never decreases. A new reservation requires the counter to equal completed reports plus abandoned reservations plus one. Existing probe, call, read, revision, execution and time limits remain unchanged. Abandonment history is append-only and cannot overlap a completed or active plan.

When authorization raises or is cancelled before the executor is called, the service can durably abandon that reservation without claiming an observation. Failure after entering the executor retains ownership until cleanup has been reconciled. Checkpoint failures never permit execution.

## Explicit recovery

`recover_pending_probe` requires the exact expected plan digest and confirmed executor cleanup. It only reopens a held, unexpired attempt with accepted source coverage, unchanged source and physical cache identity, no active source owner, no completed probes, no established executable candidate or target, and no execution generations. All source contracts, findings, journals, counters and the original expiry are preserved. The old plan is retained as abandoned without asserting that an ambiguous legacy execution never started.

The next probe consumes the next existing slot and receives a new source/runtime binding and fresh authorization. Abandoned plans are never selected as current probe evidence. Completed observations and executable manifests remain protected by their existing image binding; general migration of those objects is outside this amendment.

Already-abandoned authorization failures can be reopened through the same guarded API without duplicating history. Wrong digests, missing cleanup, active work, stale source/cache identity, expired attempts and exhausted budgets refuse without mutation. A late report for an abandoned plan is rejected.

## Verification

Synthetic tests cover journal roundtrips, authorization failure before any executor work, legacy pending recovery followed by a probe on a different image, immutable counters/deadline/history, append-only validation, late reports, exhausted allowance, failed persistence and refusal of ambiguous active or completed evidence. Public scenarios contain fictional identities only. Independent adversarial review, the full root suite, Ruff, baseline ratchet, mypy and exact-head preflight remain required before pushing.
