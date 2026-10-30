# Amendment: complete review evidence and separate repair budgets

## Status and intent

The user approved the proposed direction with “Make it so”. This is the written architectural specification for review; implementation has not started. It extends [spec.md](spec.md) and supersedes the shared-repair and reviewer-input-delivery rules in [repair-amendment.md](repair-amendment.md). The earlier implementation and its [validation results](validation.md) remain historical evidence.

Success means a complete independent coverage judgment can reach deterministic clean execution without schema corrections consuming coverage or executable repairs. It does not mean accepting an incomplete manifest or waiving a failed check. Core behavior remains language, framework and library agnostic. No constitution change is proposed.

## Observed failure

The pinned Elm experiment finished after 635.95 seconds of its 1,800-second allowance. One activation schema correction and one coverage revision consumed the shared two-repair budget. The revised candidate still pinned a linter inconsistent with the repository's CI/configuration and checked its version without exercising lint. The second reviewer requested five files, including one additional file, but omitted four of the eight mandatory manifest inputs. Assessment validation terminated before another coverage revision. No installer or clean consumer ran.

## Required behavior

### 1. Independently prepared review evidence

For every candidate review, construct a new reviewer inspection and conversation. Through that inspection, independently fetch every manifest input from the same pinned source identity. Verify every resulting hash against the manifest before requesting a judgment. Include each exact path, content and daemon-computed hash in the reviewer conversation. Do not use the discoverer's transcript, summaries or previously cached inspection ledger as independent evidence.

Mandatory evidence delivery is deterministic orchestration, not a model tool-selection task. It proves which bytes were delivered, not that a model understood them. The fresh reviewer still supplies the independent coverage judgment and may navigate the pinned inventory and read additional relevant files through the existing tools. Preserve the final assessment's full-input hash validation and citation validation as defense in depth.

All preparation consumes the original read, byte and time budgets; checkpoint reservations before fetching. Apply existing batch limits and cancellation ownership to prefetch. A missing, unreadable or changed input holds before judgment. Never silently omit, summarize or truncate mandatory evidence. Source drift cannot become a schema correction or coverage repair.

Before the model call, measure the entire serialized review conversation, including the manifest, schema and mandatory evidence, against an explicit bounded review-context byte limit. Reserve space for the configured output and enforce any available model context constraints. Record measured bytes and the configured limit. For this amendment, oversized evidence holds with a specific diagnostic; automatic partitioned review is out of scope. Additional tool results must remain within the same context bound. Provider context rejection also holds explicitly.

### 2. Separate durable repair counters

Replace the single shared repair allowance with three independent, attempt-wide limits, each defaulting to two and configurable from zero to two:

| Counter | Consumed by | Does not authorize |
| --- | --- | --- |
| Schema corrections | Malformed JSON/envelopes, invalid tool arguments and schema-invalid proposals or assessments that currently receive correction feedback | Source/hash drift, missing evidence, provenance violations or runtime uncertainty |
| Coverage revisions | A complete independently bound assessment returning `revise`, followed by another candidate | An incomplete review, `indeterminate`, or removal/weakening of required obligations |
| Executable repairs | A validated failed executable report after confirmed executor cleanup | Missing, malformed, wholly unrun or unknown reports, or unconfirmed cleanup |

The user approved correcting the Elm citation failure with “Fix it”. Invalid model-owned citation references are assessment corrections: validate them inside the existing reviewer loop, return the exact independently inspected file paths as correction guidance, and consume the existing schema allowance. The reviewer must issue a new valid response; never silently remove citations or infer acceptance. The independently fetched source/hash ledger is unchanged. Missing evidence, hash drift and identity/provenance violations remain terminal. A corrected `revise` consumes coverage allowance only after its evidence binding validates.

Each reservation is persisted before sending correction feedback or beginning the next external action. Counters never reset when a candidate changes, a reviewer starts, a cache generation changes, or the process restarts. Two schema corrections can be followed by two coverage revisions and two executable repairs, but all six remain subordinate to the existing aggregate deadline, 128-call limit and 512-read limit. Per-phase limits remain in force. Increasing the Elm experiment's time allowance does not renew any other budget.

A schema-valid candidate that removes or weakens a prior required obligation remains a terminal hold. `indeterminate` remains a hold. Budget exhaustion identifies the exhausted category; no automatic new logical attempt bypasses it.

### 3. Persisted-state compatibility

Version the attempt journal and store each category's consumed and maximum counts plus the existing original UTC expiry, identity, generation and rejected execution evidence. Validate counters and limits on rehydration and preserve strict checkpoint failure behavior. Update daemon and harness through shared helpers, not divergent implementations.

Existing accepted/frozen contracts remain readable and retain their manifest schema and canonical digest. Do not reinterpret a pending legacy journal's undifferentiated consumed repairs as unused category allowances. Pending or held legacy attempts require an explicit new operator-started attempt with confirmed prior cleanup, with a clear migration hold reason; automatic restart must not reset their budgets. Interrupted executors remain held until cleanup is established. Reject unknown or malformed journal versions safely.

### 4. Evidence conflicts and coverage obligations

Require the review judgment to identify conflicting repository constraints, omitted required lanes, and checks that prove presence/version without exercising a declared usability constraint. Return cited revision feedback to discovery. Discovery must resolve findings from pinned repository evidence or hold; core code must not select ecosystem versions or invent project commands.

Preserve every required requirement/check/service/lifecycle ID, required status and scope across schema correction, coverage revision and executable repair. A revised candidate always receives a new independent review with newly fetched mandatory inputs. New files discovered by a reviewer may motivate revision; discovery must inspect the evidence used by the revised candidate, and the next reviewer receives its complete resulting input set.

### 5. Executable acceptance remains mandatory

Only independently accepted coverage proceeds to installation. Execute all required checks and applicable service lifecycle actions in a clean consumer using the accepted digest, pinned source and immutable image/platform identities. No reviewer verdict can override failed, missing, unknown or unrun checks. Executable repair uses a fresh empty cache generation after confirmed cleanup and repeats review and every required check. Retain the existing bounded three rejected generations and rejected-report limits; coverage-only revisions do not create executable cache generations.

The opt-in validation harness still requires explicit command-scope inspection for every new accepted digest. Preserve secret references, existing redaction, original failed evidence and pinned consumer repositories without modification.

## Diagnostics

Report the actual failing stage and a distinct code for evidence preparation failure, evidence hash drift, oversized review context, schema-limit exhaustion, coverage-limit exhaustion, executable-limit exhaustion and provider/context failure. Show each consumed/max category counter, calls, reads, elapsed/remaining time and mandatory-input delivery count. Never expose raw source, secrets or model rationale in operator summaries. Preserve private detailed evidence for diagnosis.

## Acceptance scenarios

1. Replay Elm's failure sequence deterministically: one schema correction, a complete review requesting two omitted test lanes, a revised candidate, a complete review identifying conflicting tool versions and missing usability checks, then an evidence-resolved candidate. Assert both reviewers receive all eight mandatory inputs without having to request them. The final coverage revision remains available despite the earlier schema correction. Scripted acceptance is a protocol test, not evidence that real Elm passed.
2. Test each category at zero, at its limit and beyond its limit, including snapshot roundtrip and executable repair followed by fresh discovery. Consuming one category must not decrement another; no deadline/call/read renewal is allowed.
3. Missing input, changed hash, oversized initial or growing context, source-request cancellation and checkpoint failure all hold without obtaining or accepting a judgment. Confirm additional reviewer reads and citations remain validated. No execution occurs on any such hold.
4. Legacy in-progress/held journals hold without renewed allowances; accepted legacy contracts still load. Unknown/malformed state and interrupted cleanup remain closed.
5. Required IDs/scopes/lifecycle obligations cannot be removed or weakened. A simulated complete reviewer acceptance followed by failed or unrun clean checks remains unready. A validated executable repair must rerun every required action in a new generation.
6. Run focused failing-then-passing regressions locally, unchanged quality checks before push, the broad suite in CI, and fresh independent full-diff review. Retest pinned sample and Elm, labeling Elm's 1,800-second experiment separately from defaults. Preserve every actual failed result and confirm owned-resource cleanup.
7. PR #354 remains unmerged until both sample and Elm actually pass mandatory independent coverage and all required clean-consumer execution. If held, report the exact stage, evidence and remaining limits without claiming readiness.

## Scope and trade-offs

Changes are limited to review orchestration, shared attempt/journal/config helpers, their daemon/harness integration, operator diagnostics and relevant tests/docs. No new model routing, automatic review partitioning, project-specific recipes, constitution amendment, consumer-repository edits or unrelated refactors.

Independent prefetch costs reads and context on every review. Separate category limits permit more correction work than the previous shared allowance, bounded by the unchanged aggregate limits. Oversized repositories can still hold at the explicit review-context boundary; this is preferable to pretending an incomplete judgment established coverage.

## Self-review

The design preserves independent judgment while making mandatory evidence delivery deterministic. Preparation never counts as model acceptance. All correction paths retain required obligations, durable counters, original expiry and cleanup gates. Legacy state cannot silently gain repair capacity. Acceptance scenarios cover the observed Elm failure, category boundaries, context limits, restarts and false-readiness controls. Written-spec approval and a subsequently reviewed implementation plan remain prerequisites for code changes.
