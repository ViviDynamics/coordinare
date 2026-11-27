# Amendment: authoritative provenance and executable repair

Approved direction: user requested implementation of the follow-up design and another test run. This amendment preserves mandatory fresh coverage review followed by deterministic execution of every required check. It adds no language or framework rules.

## Requirements

- A discovery proposal contains only requirements, install actions, checks, activation, services and empty-environment justification. The daemon constructs the full versioned manifest from the actual read ledger, pinned commit, inventory and runtime capabilities. Agent-supplied provenance fields are forbidden. Persisted/wire manifest schema and canonical digest stay unchanged.
- A review proposal contains only verdict, cited rationale and personally inspected source paths. The daemon binds it to the exact reviewed manifest and independent read ledger. Every manifest input must be independently read, with matching actual hashes. Source drift, incomplete evidence and runtime identity changes hold.
- Repository and scratch roots are disposable and may have different absolute paths in installer, verifier and consumer. All required persisted artifacts must live in cache; persisted references to installer-only source paths are invalid. The reviewer judges portability; clean execution remains decisive.
- A valid failed execution report may request a new proposal only after the prior executor is confirmed stopped. Missing/malformed/wholly-unrun/unknown evidence cannot authorize readiness or repair. A valid report with an actual failed action may include skipped dependent checks, but every required action must run successfully in the repaired attempt. Infrastructure and unconfirmed cleanup failures hold.
- Preserve every prior required requirement/check/service and required lifecycle command ID, applicability scope and required status. New proposals undergo fresh review, use a new empty cache generation and execute every required check again. Failed digests and bounded redacted per-check evidence remain rejected audit entries. Never amend a failed result into a pass.
- Share the existing maximum two repairs, 128 model calls and 512 reads across protocol, schema, coverage and executable repairs. Persist consumed counters, original UTC expiry, rejected digest/report and current generation with the cache snapshot. Restart never resets a pending repair's budget. New pinned source identity starts a separate attempt; actual drift within an attempt holds.
- One deadline covers discovery, installation, verification and repair: the smaller positive manifest-attempt and outer bootstrap limits, capped at 3600 seconds. Defaults remain 600 seconds overall and 120 seconds per model phase. Each external operation is capped by remaining time; cancellation must reap its resources. Operator scope inspection in the opt-in harness remains mandatory for each new digest.
- Failed generation contents may be retained as bounded rejected artifacts (at most three generations per logical attempt); no active executor is retained. A repaired generation must never mount or reuse the failed generation. Normal cache provenance and retention rules apply after a successful freeze.

## Acceptance

1. Agents produce usable declarations without copying hashes; extra provenance fields are rejected. Actual source/runtime drift and incomplete independent reads still hold.
2. A synthetic installer-path-dependent artifact fails clean execution; a portable revision passes using a new cache, fresh review, same remaining budget and a new digest. Required failures cannot be removed, made optional or narrowed to a different scope.
3. Exhausted repairs/deadline, stale or missing evidence and unconfirmed cleanup hold, including after snapshot roundtrip. Docker and Kubernetes dispatch use the current cache generation.
4. Full suite runs in CI, focused TDD locally. Retest the pinned sample, Elm and all 29 baseline eligible repositories with separately labeled profiles. Do not merge or claim bootstrap fixed until sample and Elm pass required clean execution. Preserve existing failed results and private sources/transcripts.

The user's explicit instruction to implement this design authorizes the amendment and implementation work without another approval pause. Sequential implementation is retained.
