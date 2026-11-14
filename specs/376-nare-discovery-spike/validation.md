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

Runtime, evidence, checkpoint, accounting, reviewer and phase migrations;
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
the daemon uses Python3.12. Packaging regression checks pass. The full repository
suite is running; this foundation is not production wiring or acceptance.

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
