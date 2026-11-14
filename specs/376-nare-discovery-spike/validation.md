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

Production reviewer acceptance and remaining phase migrations;
daemon lifecycle tests; bounded real reviewer and installer/repair/clean-consumer
acceptance; performance measurements; deletion of the old loops; full checks,
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
The checkpoint and exact launch-code provenance are retained. An optional
five-minute continuation under the same total8-million token cap awaits explicit
authorization; final production acceptance remains required on the final code.
The existing SQLite throughput benchmark still
hits its90-second limit locally and remains a final verification gate.

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
