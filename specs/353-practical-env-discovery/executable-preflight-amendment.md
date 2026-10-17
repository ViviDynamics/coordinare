# Amendment: complete repair evidence and executable preflight

**Feature branch**: `353-practical-env-discovery`
**Created**: 2026-10-17
**Status**: Draft for written-spec review
**Issue / PR**: #353 / #354

## Intent and decisions

The user approved the alternate design with “Okay. Implement”. Improve the existing
bootstrap orchestration by supplying complete repair evidence, probing the actual
consumer image, and checking installation outputs. Keep the existing fresh-context
discovery/review conversations; do not introduce subagents. Elm remains the first
live test case. Other repositories remain paused until Elm passes.

This extends [spec.md](spec.md), [repair-amendment.md](repair-amendment.md), and
[review-evidence-amendment.md](review-evidence-amendment.md). It preserves independent
coverage review followed by deterministic execution of every required check. It
does not amend the constitution or introduce ecosystem-specific core behavior.

The latest pinned Elm run stopped after 1,243.38 seconds of a 3,600-second allowance.
Coverage was independently accepted. Installation failed because the image lacked
an assumed executable and because an action copied a directory that did not exist.
Repair reread 13 of 18 previous inputs; validation rejected its next proposal. All
18 original source hashes still matched. No clean consumer ran. These observations
motivate evidence delivery and actual runtime observation, rather than more sessions
or a larger timeout. Private evidence is retained separately from tracked docs.

## User scenarios and testing

### Story 1: Repair from complete evidence (P1)

An operator's failed bootstrap can propose a correction without depending on the
model remembering to request every previously inspected file.

**Independent test**: Give repair an 18-input manifest and no model-selected reads.
Assert all 18 files are independently fetched, hash-checked, and delivered before
the first repair call. A missing file, changed hash, or oversized context holds.

### Story 2: Discover actual runtime prerequisites (P1)

An agent can observe whether repository-required tools and capabilities exist in
the exact target image before constructing installation declarations.

**Independent test**: A nested synthetic project requires an unfamiliar executable.
A generic probe reports its absence in the pinned image; discovery declares a
repository-evidenced installation and required clean usability check. No production
branch recognizes the executable, language, directory layout, or package manager.

### Story 3: Fail installation at a missing output (P1)

Required assertions establish that an install action produced the artifacts its
dependent actions consume. A successful package-manager exit alone cannot prove
that a later copy or activation target exists.

**Independent test**: An install command exits zero without producing its expected
directory. Its required assertion fails, dependent actions are recorded unrun, and
readiness remains false. The original failure is retained for bounded repair.

## Functional requirements

### FR-001: Deterministic mandatory repair evidence

Before requesting an executable repair, create a new discovery inspection and fetch
every previous manifest input from the pinned source. Verify its actual hash against
the previous manifest and deliver exact path/content/hash records in the initial
repair context. Do not substitute the previous transcript, manifest hashes alone,
or an agent summary. Additional source navigation remains available.

Use the same bounded preparation mechanism as independent review. Count all reads,
bytes, and time against the existing attempt; persist reservations before fetching.
Apply the configured serialized context bound to the complete repair conversation
and every subsequent tool result, without truncation. Missing evidence and actual
drift have distinct diagnostics. Neither permits executing a partial proposal.
Keep final full-input validation as defense in depth. Independent review still
performs its own fetches after repair; delivery is not model acceptance.

### FR-002: Generic bounded probe proposals

Add a versioned probe proposal/report contract separate from the accepted environment
manifest. A proposal contains up to 16 unique commands using the existing explicit
argv/cwd/env command vocabulary, source citations, and a stated capability question.
Each citation must have been read from the pinned source. No language or tool catalog
selects commands. Discovery may request at most two probe batches per logical attempt,
including executable repair; malformed proposals use the existing schema budget.

Probe commands have no services, background execution contract, installation role,
or readiness authority. They run before final manifest construction. Agent intent
does not establish read-only behavior: isolation enforces the runtime boundary.
No model-selected command may execute on the daemon host.

### FR-003: Disposable probe execution

Run probes through the existing consumer platform adapter in the same immutable
image/platform identified by execution capabilities. Use pinned source mounted
read-only, an empty read-only cache, and disposable writable scratch. Disable network,
omit runtime secrets and host sockets, and use the adapter's existing execution
restrictions. Resolve explicit roots without inheriting shell startup hooks.

Limit each command to 30 seconds and a batch to 120 seconds, both capped by remaining
attempt time. Keep at most one probe executor active per attempt. Capture at most
16 KiB combined stdout/stderr per command and 256 KiB per batch. Record truncation
explicitly: truncated or timed-out output cannot establish a positive capability.

An executed nonzero exit is an observation, not an environment-check waiver or an
automatic schema repair. Preserve structured status, exit code, bounded redacted
output, and actual source/image/platform/proposal identities. Absent execution,
unsupported isolation, malformed evidence, or uncertain cleanup holds. Do not silently
skip probes on an adapter that cannot enforce the contract. Confirm resource removal
before any subsequent probe, installer, or repair execution.

### FR-004: Probe results inform declarations, never readiness

Return probe reports as untrusted observations to discovery and repair. Require the
manifest proposal to resolve observed missing prerequisites with evidence-backed
installation declarations or an explicit hold. Do not interpret arbitrary stdout
as instructions or let model-generated report fields stand in for execution.

Bind a digest of validated probe evidence into the independent review request and
the persisted attempt audit. The review still assesses repository coverage against
the complete manifest, including probe findings and installation portability. The
manifest's source inputs remain exclusively repository file hashes; probe reports
are runtime evidence, not synthetic repository inputs.

A probe is not an installed-state test. The final manifest must retain required
clean-consumer checks for all declared constraints, including those probed earlier.
Installer and clean consumer execute the same accepted manifest digest. Current
image/platform identity must still match at execution; changes invalidate admission.

### FR-005: Executable installation postconditions

Use existing required install commands and dependency edges to declare assertions
immediately after producers and before their consumers. For every persisted artifact
that a later install action, activation, or clean check depends on, discovery must
declare a required executable postcondition establishing the necessary property.
Presence, executability, and portability requirements come from repository evidence
and consumer use; core code supplies no predefined artifact recipes.

Assertion commands are ordinary manifest actions with stable IDs. They are covered
by review, the canonical digest, execution evidence, and repair obligation retention.
Dependencies must make consumers wait for the relevant assertions. An action exiting
zero without its required assertion passing cannot admit dependent work. Every
required clean check still runs after successful installation in the separate clean
consumer; installer assertions do not substitute for clean verification. This design
requires no new artifact filename catalog or change to the accepted manifest schema.

### FR-006: Shared budgets, journal, and cancellation

All phases share the original expiry, 128-call/512-read limits, and independent
schema/coverage/executable repair counters. Defaults remain unchanged. The explicit
Elm experiment continues to use a fresh 3,600-second aggregate allowance; no active
or expired attempt is renewed. Probe limits are additional bounds, not extra time.

Persist probe proposals, reserved batches, report digests, and executor ownership
before external work. Keep at most two bounded reports per attempt; model-visible
context admission includes them. Resume must not refund batches or replay an
uncertain executor. Cancellation stops owned work and confirms cleanup or holds.

Version the mutable attempt journal when adding probe ownership/counters. Existing
accepted frozen contracts remain readable. Pending older journals require explicit
operator restart after confirmed cleanup; never silently grant new capacity.
Reuse existing bounded rejected-generation retention and operator reset behavior.

### FR-007: Integration and safe diagnostics

Daemon and validation harness use the same evidence preparation, probe admission,
runner, journal, and report validation. The harness requires explicit command-scope
inspection before each probe proposal executes and before each accepted manifest
digest executes. Approval of one never approves the other.

Report actual terminal stage, elapsed time, calls/reads/bytes, repair counters, probe
batches, mandatory input delivery, and safe failure codes. In particular, failure
during repair discovery must not retain the earlier installer stage. Raw source,
credentials, transcripts, and model rationale stay out of operator summaries.
Use runtime secret references and existing redaction throughout.

## Acceptance and success criteria

1. Reproduce the 13/18 repair omission with a deterministic fixture, then prove all
   18 mandatory inputs are delivered without model-selected rereads. Missing/drifted
   inputs, checkpoint failure, cancellation, initial/growing context overflow, and
   exhausted reads all hold before the next external action.
2. Exercise an unfamiliar executable and nested project. A real disposable probe
   reports absence; installation can correct it through generic declarations. No
   stack-specific production branch is added. Probe findings reach fresh review.
3. Test probe schema, source citations, maximum batches/commands/time/output, strict
   identities, no network/secrets/writable source/cache, actual adapter cleanup, and
   interrupted-journal recovery. Unsupported adapters hold without fallback.
4. Reproduce a zero-exit producer with a missing artifact. Assert required postcondition
   failure, blocked dependent execution, preserved report, fresh repair generation,
   stable required IDs/scopes/lifecycle obligations, and full clean re-execution.
5. Probe success and reviewer acceptance cannot waive failed, absent, unknown, skipped,
   or unrun required execution. Forged, stale, or truncated probe evidence cannot
   authorize a positive capability or readiness. Installer-only changes remain absent
   from the clean consumer and must be installed portably in cache.
6. Verify shared counters and original expiry across probe batches, schema corrections,
   coverage revisions, execution repairs, process restart, and snapshot roundtrip.
   Measure actual calls, bytes, and elapsed time against the specified budgets.
7. Focused local failing-then-passing tests, unchanged quality checks, broad suite via
   CI, and independent full-diff review precede merge. Run `speckit.analyze` against
   the written implementation plan before implementation.
8. Retest only the pinned Elm snapshot first, using the same model
   `onprem/glm-5.3-flash`, low reasoning, output allowance 32,768, and explicitly labeled
   3,600-second profile. Preserve all earlier failures. Report discovery, probes,
   independent review, installation, clean execution, readiness, and owned cleanup
   separately. Do not resume the other repositories until Elm actually passes.
9. Existing sample-plus-Elm live-success merge criteria remain mandatory. Green CI,
   synthetic acceptance, a longer timeout, or a probe result cannot satisfy them.

## Scope, alternatives, and trade-offs

Implement complete repair evidence first, then generic probes and installation
postconditions. Keep the existing model transport and fresh conversations. More
subagents would preserve the missing-evidence problem; larger timeouts do not repair
it. Image-specific tool catalogs would violate ecosystem neutrality.

Changes are limited to bootstrap evidence/orchestration, generic adapter execution,
attempt persistence/configuration, harness diagnostics, and relevant tests/docs.
No consumer-source changes, automatic deployment, new model routing, arbitrary host
execution, review partitioning, constitution change, or unrelated refactor is included.

Prefetch adds reads/context, and probes add bounded latency and adapter work. They
cannot guarantee that a model proposes a correct manifest. Oversized evidence,
unsupported isolation, unavailable prerequisites, and unresolved conflicting source
requirements can still hold. Those holds must remain explicit and actionable.

## Self-review

Reviewed against the existing constitution, generic command schema, attempt budgets,
repair/review amendments, and the observed Elm failure. Artifact assertions reuse
existing action dependencies; probe evidence has separate identities and authority.
No mandatory evidence or execution gate is relaxed. Written-spec and subsequent
implementation-plan review remain outstanding; no product code accompanies this draft.
