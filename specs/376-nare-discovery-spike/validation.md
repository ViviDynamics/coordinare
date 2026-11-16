# Production integration validation

Implementation is in progress. No production readiness or end-to-end pass is claimed.

## Baseline

The implementation branch was created from main at `b05b418b` and locally merged
exact prerequisite `31d54cd23d0da39e727ae0855b0ac9424cbea09b` without conflicts
(merge `974df0fa`). Only the approved documentation commits were carried from the
spike. The frozen validation checkout, spike artifacts and PR355 were not modified.

The affected bootstrap baseline completed with **2,370 passing tests** and
18 existing warnings. Initial collection required installing the performer test
package, as documented by the repository preflight commands. No tests were skipped
or suppressed to pass this baseline.

## Dependency provenance

Nare distributes official wheels through GitHub releases, not PyPI. The published
`2026.11.104` wheel targets tested source commit
`21fd8e7fc010137dd948aef717f16eabb7f359f9`. Its SHA256 is
`fdb7fcf20efe16081298e0787ddfcc83e4f23e5f7c998bf51c42e74de4db4c1b`.
All 31 packaged Python source files other than the generated version module match
that source commit byte for byte. The normal dependency resolver selected
anthropic2.8.0, mcp2.3.0 and pillow13.0.0. No sibling source paths are required.

## Configuration discovery

The daemon's existing bootstrap path selects `conducting`, through
`_get_manifest_llm_chat`. It does not select the performer mode catalog.
The replacement must preserve this route and its explicit model/parameters;
CLI-only choices must refuse before inference. Native Anthropic support must
explicitly reject parameters its native transport cannot preserve.

## Remaining gates

Native phase migration and deterministic daemon lifecycle checks are implemented.
Live reviewer and pinned installer/repair/clean-consumer acceptance, final-code
performance verification, deletion of the old loops, full repository checks,
adversarial review and Copilot review remain incomplete.

## Runtime foundation

Nine runtime/configuration tests pass, including a request captured at the actual
native OpenAI HTTP boundary. Real nare admission accepts only after the domain
check; rejection and checker errors cannot produce accepted output. Built-in host
tools are absent. Unsupported transports, missing selected credentials and
unsupported Anthropic temperature are refused explicitly.

The normal wheel and daemon image both build. An isolated wheel consumer and a
network-disabled daemon container import the runtime and report nare2026.11.104;
the daemon uses Python3.12. Packaging regression checks pass. The initial full repository result and subsequent fixes are recorded below;
this foundation alone is not production wiring or acceptance.

## Evidence foundation

Eight evidence tests pass with exact transport-boundary content checks, Unicode
and escaping under the serialized cap, partial responses, pinned-hash refusal,
scoped requirements, invalid requests and empty sources. A real nare conversation
rejects an early answer, delivers all required pages, compacts and rereads without
losing coverage. Evidence delivery does not establish review validity or readiness.

## Checkpoint foundation

Seventeen checkpoint tests pass, including all identity dimensions, file tampering,
interrupted atomic writes, pending probe/trial holds, pinned catalog restoration
and real nare continuation after interrupted inference. Continuation retains
usage, turns and corrections and does not replay the completed evidence tool.
The affected checkpoint/evidence/runtime and existing journal suites total
96 passing tests. Phase wiring and external-effect reconciliation remain later gates.

## Accounting foundation

The native runtime reserves and durably records each request before inference,
including nare retries. Completed usage has persistent receipt deduplication;
missing provider usage remains unknown. Schema corrections preserve separate
scope, executable and execution repair allowances. Cancellation closes the stream,
and terminal timeout reports cannot inherit a working session's status.
Existing journals retain prior usage when loaded without the new native fields.
The focused runtime, accounting, transcript and repair-journal checks pass
(150 tests); strict typechecking passes across 365 modules. Actual daemon activity
wiring remains Task7. Nare's Anthropic SDK has its internal retries disabled.

The initial full repository run finished with 19,430 passed, 97 skipped,
589 deselected and nine failures. Dependency audit additions, existing test timing
and dated fixtures, untracked-file discovery, and mixed-version module collection
are being investigated; the full suite is not claimed green.

## Native reviewer integration (in progress)

Shared domain criteria and the final assessment schema are preserved separately
from transport instructions. Scripted native tests cover early refusal, real
accept/revise/indeterminate admission, citation refusal, independent role identity,
phase deadlines, source navigation and scope observation. Phase call reservations
survive domain restart and checkpoint rewind is refused. Bounded safe progress
reports delivered pages without source text.

The combined native and existing bootstrap regression suite passes2,434 tests.
Native schemas retain ledger-specific decision cardinality and finding-ID constraints. Ruff and strict typechecking pass
across366 modules. One bounded production-adapter saved-packet replay reached all
386 complete pages from148 pinned source files, then exhausted its20-minute limit
before assessment. It reported5,147,093 tokens, six compactions, zero transport
retries and one interrupted request with unknown usage. No live verdict is claimed.
The checkpoint and exact launch-code provenance are retained. The subsequently approved five-minute continuation and its metadata failure are
recorded below; final production acceptance remains required on the final code.
The SQLite runtime-environment difference and successful unchanged Python3.12
benchmark are also recorded below.

## Native scope, authoring and repair (Task6)

All source scope roles, divided global/group construction, scoped review and
delta-to-full repair now have native phase adapters. Scripted transports exercise
real nare and unchanged domain validators. Native probes/trials reserve durable
effect receipts before execution; uncertain effects cannot replay. Complete
source/observation pages and their delivery coverage survive checkpoints, and
trial/sharded-correction spending survives the domain journal. Preflight retains
one free reminder, without renewing it on resume. Terminal scope reopening
propagates to the domain scheduler.

Validation: 2,464 affected bootstrap/native tests passed with 18 existing warnings;
the subsequent journal counter fix passed 84 journal/trial/checkpoint regressions.
Targeted ruff and strict mypy (368 modules) passed. These provider-scripted tests
prove adapter/domain behavior, not real installation or environment readiness.
Daemon lifecycle and final live installer/consumer acceptance remain outstanding.

The old partial-JSON/decision-carry correction heuristic is not reimplemented:
nare owns correction conversation, final schemas still require every decision,
and existing domain ledgers/frozen commands remain authoritative. Recovery cost
and quality must be assessed in final live acceptance. Legacy runtime deletion
is deferred until that acceptance passes.

## Daemon lifecycle (Task7)

The normal daemon route selects its configured conducting model and starts native
scope/author/reviewer sessions. One preparation owner per symphony prevents a
second cycle from clearing live source ownership. Cancellation reaches the caller,
cleans owned source state and produces a visible terminal hold. Private checkpoints
refuse all overlap with performer mounts. Verified provider-error sessions resume
through a real daemon snapshot round-trip without renewing attempt or phase limits.
An older verified cache retains its clean-consumer evidence and provenance on restart;
incomplete legacy conversations create no native delivery receipts. Unknown past
trial spending is reserved conservatively instead of renewed.

Validation: 2,589 affected bootstrap/native/lifecycle tests passed (21 warnings).
Final focused native/status/control checks passed 209 tests (one warning); targeted
ruff and strict mypy (369 modules) passed. The final checks include serialized status
inputs, client cleanup failure, source navigation policy and exact delivery receipts.
These are deterministic provider tests and local fixture execution. Live production
installation/repair and final runtime retirement remain Task8 gates.

## Acceptance preparation (Task8, incomplete)

The private repository field harness now defaults to the native configured route,
records normal conductor transcripts, and preserves existing installation,
approval, cleanup and provenance gates. Deterministic daemon tests execute a real
local failed installer/check, native delta repair, separate consumer check and
provenance freeze; altered payload/source is refused. Provider answers and the
container boundary remain substituted in these fixtures. This is not the pinned
live-project acceptance required for deletion or readiness.

Offline performance measurements on one runner: complete four-MiB paging median
72.44 ms, peak incremental allocation 9,583,943 bytes; cold daemon import median
0.82557 s for the prerequisite baseline and 0.83194 s for the candidate. Immutable
encoded-page caching removed repeated serialization while exact page equality and
receipt tests remained green. The existing advisory benchmark lane now also runs
these budgets against the PR's pinned base on the same runner.

The unchanged SQLite throughput test passed in35.82 seconds under Python3.12.15
with SQLite3.54.1, after repeated90-second timeouts under Python3.14.9 with
SQLite3.53.1. Local validation now uses Python3.12 to match CI. No SQLite product
code, assertion or timeout was changed; the full canonical suite is still required.

The explicitly approved saved-review continuation and one retry both stopped before
inference when model metadata returned HTTP502. The library substituted a32,000-token
window for the unavailable metadata, smaller than the saved73,383-token estimate.
Additional model requests/tokens were zero. No assessment or readiness is claimed.
The production adapter now holds with `agent_context_unavailable` when context
metadata is missing or invalid, before inference or implicit fallback. Final live
review and installer/consumer acceptance remain outstanding. No old loop is deleted.

The first repository preflight failed the existing lint-debt ratchet. Native
checkpoint, source-phase, transport and dispatch preparation were split into
focused helpers and typed contracts, without changing the baseline or suppressing
rules. The affected regression passed 2,530 tests; checkpoint follow-up passed 30.
Whole-tree lint, the ratchet and strict typechecking across 370 modules passed.
After the import refactor, paging measured74.97ms and9,583,943bytes; same-runner
cold imports measured0.92915s baseline and0.98759s candidate. Both unchanged
performance budgets passed. Full repository preflight is still pending.

Preflight2 completed with lint, the unchanged ratchet, typechecking and changed
unit tests passing. The full suite reported19,562 passed,96 skipped and589 deselected,
with one failure: the cold-start benchmark required an explicit baseline path that
normal preflight does not supply. Its setup now archives an immutable local
merge-base or parent when no path is configured; Test/Coverage CI checkouts retain
the parent, while the dedicated benchmark lane continues to use the exact PR base.
Each import also verifies that the daemon came from the selected source tree.
Five focused checks passed with automatic selection; explicit selection also passed.
No benchmark was skipped and no threshold was relaxed. A busy-host paging sample
exceeded100ms; the subsequent isolated median was75.39ms. Startup measured0.87004s
against main's baseline and1.00429s for the candidate; explicit prerequisite-base
measurement was0.92286s against1.00798s. Full preflight must run again on this fix.

The approved saved-packet continuation subsequently completed in197.66 seconds.
It consumed385,538 additional tokens (5,532,631 total reported tokens, plus a retained
294,912-token reserve for the interrupted parent request), with zero transport
retries. A fresh validator invocation rehashed148 source files and verified386
delivered pages,24 citations, three prior finding decisions and one new finding.
The genuine verdict was `revise`. Structural admission does not establish review
accuracy: the new finding appears to cite a historical failure despite the current
candidate already declaring the requested compile-and-run probe. This remains a
quality concern. The replay used frozen launch code and executed no installer or
consumer; final-head live acceptance and legacy-loop deletion remain outstanding.

Preflight3 passed all six configured rows on the subsequent candidate in1,624
seconds, including the full Coordinare and performer suites. The first production
native bootstrap acceptance run then reached an independently accepted manifest
and executed the installer:24 checks passed and5 failed. All installation steps,
RSpec, Rails boot, database and service lifecycle checks passed; remaining failures
included a missing Ruby require, a duplicated test path, an unavailable Node CLI,
a missing database for seeds and a separate service connection check. The clean
consumer did not run, so this is not environment readiness.

The first repair generation stopped at its unchanged eight-correction allowance
with `scope_required_check_mapping`, after159 total model calls and zero transport
retries. Owned runtime containers, registry and temporary image references were
verified absent; the pinned source snapshot was unchanged. Private artifacts remain
preserved. Diagnosis found that the native adapter retained generic guidance while
dropping the domain validator's sanitized missing-criterion and colliding-ID details.
The saved correction history repeatedly asked the author to fix unspecified IDs.
Three real-validator tests reproduced the loss at the next nare model request;
the adapter now serializes the existing sanitized error into its rendered message.
Thirty focused checks pass without changing admission rules or correction budgets.
Broader regression and a new live run remain necessary; this fix does not establish
that all five execution failures have been repaired. Legacy loops remain present.

The feedback fix passed2,495 affected regressions. A second production-path test
then exposed an exhaustion-routing defect: native schema errors triggered the
existing delta-to-full repair fallback, but repeated domain mapping errors returned
the last rejection code instead of `attempt_schema_limit`. The runtime now records
the domain rejection in validation diagnostics and reports correction exhaustion
using the existing budget code. The real delta/full pipeline test now covers both
schema and domain rejection, preserves frozen checks and independent review, and
proves the full repair retains the already-spent correction count. No allowance is
reset and a completed rejected checkpoint is not resumed.

The stopped second native run exposed an evidence-navigation failure: repeated
invalid page ranges consumed calls while delivering almost no additional evidence.
The replacement protocol gives forward sequencing to the runtime and separates
explicit rereads. Whole-page delivery and required-evidence admission remain
unchanged. A separate attempt-wide allowance holds after eight invalid evidence
calls, including native schema refusals, with persisted deduplicated receipts.
Successful reads do not replenish the allowance; valid rereads do not consume it.

Against an isolated normally installed candidate nare wheel, all 2,510 affected
353/376 unit and integration tests pass in 133.94 seconds. This includes recovery
from invalid navigation through real native dispatch to the real domain validator,
exact source bytes, repeated-error exhaustion, valid rereads, journal persistence
and zero-inference refusal on exhausted resume. A batch test records the important
limit: already dispatched sibling actions may run before result-event enforcement;
no subsequent model request is made after exhaustion.

The candidate dependency has passed its own full preflight and independent review,
including a compatibility fix preserving existing ToolError positional arguments.
Its published release remains pending. These offline results do not establish
installer success, clean-consumer acceptance or environment readiness. The original
stopped run and its accounting remain preserved; no checkpoint was reset.
Candidate performance checks also pass with unchanged limits: complete 4 MiB paging
has a 74.16 ms median and 9,583,943-byte peak; cold start is 0.9524 s against 0.8459 s
for the immutable local baseline, within the existing 1.1x+0.1 s allowance.

Recovery now pins official nare 2026.11.105, released automatically from merged
commit `07207e61f6a3b9d7b45d173a01b1a48df2b93870` after upstream PR 309 passed
all seven checks. The downloaded wheel SHA256 is
`becd13ed799cc718995e3a093edb17a350f9ae90ea565a9f9227f3e92aaa5d10`.
All 32 packaged Python files other than the generated version module match that
commit exactly; LICENSE and NOTICE also match. Normal resolution changes only
nare in the lockfile, and the production virtual environment reports 2026.11.105.

Independent recovery review found three accounting/resume issues. New failing
then passing tests cover interrupted batches retaining completed invalid-call
charges, domain-held checkpoints refusing inference despite native working status,
and duplicate tool-call identities refusing before dispatch in both streaming and
ordinary transports. Clean-interruption resume remains supported and retains its
spent allowance. Already completed invalid calls in a batch can exceed eight;
the exact consumption is preserved and prevents any subsequent inference.


## Response and transport contract audit

A parallel audit after the held native trials identified correction accounting,
stop-reason translation, source-only tool guidance and answer-encoding contract
gaps. Offline reproductions now cover uncharged corrections, queued corrections
at cancellation, zero-budget terminal holds, provider cutoff/refusal labels,
and preserving fatal bound-source failures while reporting unavailable new paths.
Pure reviewer diagnostics report independent structural, citation, UTF-8 and
required-read issues together before unchanged final admission. They do not
normalize answers, add a correction producer or increase any allowance.

Exact duplicate source bodies in known native packet fields now use canonical
path/digest references. Replacement requires the recorded digest, cached content
and recomputed digest to agree. Canonical bytes and mandatory receipts remain
required; legacy packets and unmatched records retain their original content.
Investigation evidence can be reconstructed to its original bound digest.

The isolated candidate regression passed **2,583 tests** with 18 existing warnings
in 346.16 seconds. The preceding run's eight integration failures were test
transports missing the now-required output capability; the fixture signatures and
capabilities were updated without weakening production checks. All 14 lifecycle
and acceptance tests passed before the full rerun. Whole Ruff, the unchanged debt
ratchet and strict typechecking across 372 modules pass.

Nare PR313 merged after seven checks and an exact-commit preflight. It adds explicit
answer encodings and restores evidence tools after domain corrections within the
same allowance. Independent review exposed two streaming gaps, both reproduced
and fixed: forced-function repetition detection and malformed fragments retaining
known usage. Optional null fragments and legacy transport signatures are covered.
The candidate wheel remains isolated until its official release is verified.
Production currently pins verified release106 from commit90c33c749, SHA256
`41e33ce60b7347e8ee82f82b60f781ad20068be6def72250338814a0ef48769d`.
All 32 source files plus LICENSE and NOTICE match the release commit.

These checks establish offline contract behavior. Both project-specific live
installer/repair/clean-consumer/provenance gates remain unfulfilled. No terminal
trial was resumed, allowance reset, legacy loop removed or readiness asserted.


Official release **2026.11.107** is now verified and pinned through the normal
resolver. Its tag targets merged commit
`4baaeb8527478401cbd7bf54b74f48c286d8a093`; the wheel SHA256 is
`8b00e7a098c2a9f87357fcb0893b6f86f06191074beb2d33b9e3b93e09eea509`.
All 32 packaged Python source files other than the generated version module,
LICENSE and NOTICE match that commit. The lockfile changes only nare, and both
the installed distribution and runtime report 2026.11.107. No source patch or
sibling import path is used in production.

The final candidate's unchanged performance gates passed: 4 MiB paging median
77.094 ms, peak 9,583,943 bytes; cold-start median 1.2612 s versus 1.3572 s for
the immutable baseline. Full exact-commit repository preflight remains the next
gate before another bounded acceptance run.
The normally installed 107 wheel also passes all 213 native unit/integration
checks. Whole Ruff, the unchanged debt ratchet and strict typing across 372
modules pass against that published dependency.

The runtime-controlled-read prototype now has a production request-stage seam:
selection precedes accounting and call recording, and the same selection rejects
out-of-stage tool calls before native dispatch. Known physical usage is recorded
before refusal, on both plain and streaming paths. The original declared-schema
context-budget floor remains enforced. Eight new checks were developed RED/GREEN;
all 221 native unit/integration checks pass, whole lint and the unchanged debt
ratchet pass, and mypy passes across 373 modules. This slice does not yet activate
the read controller in bootstrap phases or establish live acceptance. Next wire
the phase controller, policy-bound identity, and explicit transport capability.

Required-read control is now wired into every native bootstrap phase. The policy
is bound before checkpoint lookup; packet, already-requested sources and existing
observations use their original complete-page tools and receipts. All original
schemas, domain validators and budgets remain. The wider bootstrap regression
passed 2,616 tests with 18 existing warnings in 140.09 seconds. Twenty-eight
controller tests pass through the production phase and native loop, including
plain/SSE OpenAI wire requests for all three answer encodings. A subsequent
Anthropic wire test verifies `any` for the configured non-thinking, non-streaming
transport. Thinking/streaming Anthropic combinations refuse explicitly before
inference; no provider or thinking-mode fallback is used. These are offline
contract results, not live installer or clean-consumer acceptance.
The final native/benchmark run reported 250 passes and one genuine cold-start
failure caused by eager provider-SDK imports in the new controller. Moving SDK
type resolution to transport validation fixed it without changing the threshold:
both original benchmarks and all 28 controller checks pass (30 total, 13.12s).
Whole lint, the unchanged lint-debt ratchet and mypy across 374 modules pass.
### Evidence retrieval after context elision

A bounded native trial reached a valid indeterminate assessment claiming the
candidate was absent. Private checkpoint reconstruction verified the complete
phase-packet hash and candidate equality: all candidate pages had been delivered,
but none remained in the final active context after elision. Delivery receipts
therefore did their job, while the reviewer failed to retrieve the candidate.
The trial ran no installer or clean consumer; owned cleanup was verified.

The recovery change supplies a bounded, durable packet-field inventory and a
generic immutable-evidence locator. It locates source identities or literal text
and returns page references for the existing reread tool. It neither invents a
summary nor overrides domain judgments, required reads, or budgets. The navigation
policy is bound before checkpoint selection.

Five retrieval cases first failed, then passed; two durable-inventory cases also
failed before implementation and passed afterward. The real nare compactor and
production admission path run with scripted inference. Exact cross-page Unicode
retrieval, bounded cursor results, all three verdicts, durable inventory and
attempt-wide invalid-call accounting pass. Twelve focused cases plus both
unchanged performance benchmarks pass (14 total, 12.77 seconds). The native suite
passed 256 cases before the five additional invalid-call cases. Live semantic
acceptance remains outstanding; these tests establish retrieval mechanics.
The broader bootstrap regression run passed 2,631 tests with 18 existing warnings
in 137.48 seconds. A subsequent boundary test exposed machine-integer overflow
for an enormous lookup cursor; both catalog and search cases failed before the
bounded empty-result fix. All 14 retrieval cases then passed in 2.05 seconds.
Whole Ruff, the unchanged lint-debt ratchet and mypy (375 source files) pass.
The final committed head still requires full repository preflight and live
acceptance; no successful environment or semantic review is claimed.
