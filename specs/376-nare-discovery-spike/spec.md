# Nare bootstrap runtime replacement

Status: maintainer-approved design, 2026-11-14.
Authority: [approved design](nare-bootstrap-design.md). Historical authoring results
in research.md are evidence, not a veto on the newly approved integration.

## User stories

- **US1 / P1: Reliable evidence-backed review.** A large saved review packet crosses
  native tool calls without loss, compacts, rejects premature answers, and returns
  only an assessment admitted by Coordinare. Author and reviewer sessions are separate.
- **US2 / P1: Complete bootstrap through nare.** Scope, sharded authoring/review,
  manifest construction and execution repair all use the same runtime while
  existing obligation, budget, authorization and cleanup rules remain authoritative.
- **US3 / P1: Restart and terminal outcomes.** Operators can resume compatible
  checkpoints without resetting coverage or allowances; tampering, stale identity,
  uncertain external work and exhausted budgets hold closed with visible reasons.
- **US4 / P2: Remove obsolete harness code.** After replacement acceptance, the
  final bootstrap path contains no old chat loop or fallback switch. Subsequent
  bespoke-task candidates are documented, not migrated in this change.

## Functional requirements

- FR01: Nare owns bootstrap conversation mechanics; Coordinare owns domain policy.
- FR02: Preserve pinned source hashes, phase-required evidence, schema constraints,
  independent reviewers, finding/obligation ledgers and real admission callbacks.
- FR03: Deliver complete serialized pages within the runtime cap; never infer
  delivery from a request or use redacted event text as exact source evidence.
- FR04: Persist versioned identity-bound checkpoints and tool receipts; reconcile
  pending effects before resume, preserving usage, turns and correction counters.
- FR05: Preserve all existing attempt/phase/read/probe/repair budgets and deadlines;
  retries, cancellations and resumes cannot refresh them. No duplicate accounting.
- FR06: Preserve endpoint/model selection; explicitly refuse unsupported transports
  before inference. Pin a normal distributable nare dependency, no sibling paths.
- FR07: Bridge runtime activity/usage/compaction/failure into existing observability;
  no secrets in diagnostics. A timed-out working checkpoint is not a running phase.
- FR08: Native tools retain existing probe/trial authorization, cleanup and
  idempotency. Domain delta-to-full repair remains, both paths using nare.
- FR09: Prove real installation, repair, clean consumer and provenance, including
  daemon restart/cancellation. Model acceptance alone is not readiness.
- FR10: Delete obsolete bootstrap conversation loops after replacement acceptance;
  validate the final deletion diff, retain no runtime fallback, preserve shared utilities.
- FR11: Survey service inference, configuration assistant and observer for later
  migration; do not impose agent loops on one-shot classifiers.

## Constraints and edge cases

Python >=3.12; asyncio, pydantic v2, existing config catalogs. Project ecosystem
neutrality and no daemon-side clone remain mandatory. Large Unicode pages,
redaction, capped tool responses, missing source, phase identity mismatch,
provider disconnects, in-flight probes, expired deadlines and partial persistence
must have deterministic tests. No private source/transcripts in tracked fixtures.

## Acceptance

All FRs map to tasks and deterministic tests. One bounded live reviewer run plus
real fresh authoring/repair/clean-consumer acceptance passes on the final code;
source/manifest/image provenance and cleanup are independently checked. Required
lint/test/build checks and adversarial final-diff review pass. Report measured
latency/token usage without claiming universal cost reduction. Follow the detailed
acceptance and deletion gates in the approved design.


## Performance success criteria

The plan's proposed implementation budgets are100ms median paging of a4MiB
synthetic catalog,20MiB peak incremental paging allocation, and cold-start median
regression <=10% plus100ms against the recorded baseline on the same CI runner.
Measure these offline and automate regression checks. Model latency and tokens are
reported separately; existing configured deadlines/counters remain authoritative.
Immutable evidence cache keys include pinned content identity; identity changes
invalidate checkpoint eligibility. No provider cache benefit is assumed.
