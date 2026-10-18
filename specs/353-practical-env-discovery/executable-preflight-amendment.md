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
   `spark/glm-5.3-flash`, low reasoning, output allowance 32,768, and explicitly labeled
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

## Approved correction after the first Elm run

The user directed “Fix it” after the 237.66-second Elm hold. Production bootstrap
and the shared validation harness now require at least one complete, validated
capability-probe batch before accepting a manifest. A premature manifest receives
one explicit preflight-stage reminder without consuming schema corrections; another
attempt to skip preflight holds. Existing call/read/time/batch limits do not renew.
Prior completed observations may serve a repair only when their source, image,
platform, completion and cleanup evidence still validate. Direct low-level discovery
callers retain optional probing; the production service always requires it.

A bare review object containing exactly the three schema-valid fields `verdict`,
`rationale` and `sources` is normalized to the existing assessment envelope before
validation. No judgment, citation or provenance is invented. Extra authority/tool
fields, ambiguous mixtures, malformed values and duplicate keys remain rejected.
All independent evidence binding and coverage/executable gates still apply. This
prevents a valid coverage revision from spending an exhausted schema allowance merely
because its outer wrapper was absent.

The next Elm run held after 39.78 seconds: three model calls, no source reads and no
probe execution. The request advertised both large schemas simultaneously, and
correction feedback directed malformed discovery replies toward a manifest even
before preflight. The same authorized correction now gives each stage its own
system guidance: source tools first, a complete `ProbeEnvelope` after inspected
evidence, then a complete `ManifestEnvelope` after actual validated observations.
Source tools remain available throughout discovery. Feedback requests the current
stage's envelope; complete schemas describe the outer wrapper as well as its body.
The original conversation, evidence, deadlines and counters persist across stages.
No malformed envelope is silently accepted or given additional repair allowance.

The stage-specific Elm run then inspected eight files and proposed probes, but held
after 167.5 seconds on constraints absent from the advertised schema. Probe commands
now advertise the existing `required=true`, empty scope and empty dependency rules
as field constraints. The validator continues to reject optional, scoped and dependent
observations. Citation/question maps explicitly require exactly the command IDs; safe
typed error codes distinguish mismatched maps, missing questions and optional commands
without echoing untrusted values. No correction limit or readiness rule changes.

After the first executed probe batch, Elm reached independent coverage acceptance
and installation, then held after 704.79 seconds. Go, Node, module downloads and
Python tooling installed; the linter build lacked `ld`, and the dependency-free
console did not produce the copied directory. The runner also reported its built-in
missing-activation-path failure, which repair admission incorrectly classified as
an unknown command. Repair now recognizes only that known failure shape: declared
activation paths, required failed `activation` result, no exit code, exact manifest
digest and full scope. It remains rejected execution, never readiness evidence.
Unknown IDs, malformed activation evidence and indeterminate outcomes still hold.
All required command records, confirmed cleanup, existing repair limits, new empty
cache generation, independent review and complete clean execution remain mandatory.
The failed profile is preserved; a fresh bounded Elm-only test evaluates the fix.

The next Elm attempt reached valid execution repair admission, then held after
454.32 seconds because deleting owner-read-only package-cache directories failed.
A direct Docker reproduction confirmed `PermissionError`; relative bind paths were
not the cause. The harness cleanup helper now restores owner permissions only on
directories within its owned case root, without following symlinks, before removing
contents. It still requires successful removal and confirmed cleanup-container
termination before starting a new cache generation. Cleanup failure remains a hold;
no platform, language or package-manager recipe is introduced.

The same recorded failure showed another bounded-evidence defect: taking the first
512 characters of the runner's captured output tail discarded the terminal error
and kept download progress. Repair feedback now retains the final 512 characters
after redaction. The output bound and original-report digest remain unchanged;
failed/unrun statuses are preserved. A deterministic verbose-output regression and
a read-only replay of Elm's exact report verify that the terminal error reaches
repair without increasing its authority, size or budget.

The following Elm attempt successfully cleaned the failed generation and entered
execution repair. Its fresh reviewer identified real linter/Node version conflicts,
but combined `assessment` with a separate `tool: null` field after the existing two
schema corrections had been spent on invalid source reads/citations. It held after
641.6 seconds, with no timeout or clean verification. Review guidance now advertises
the complete closed `AssessmentEnvelope` and explicitly requires choosing one source
tool or one assessment, without extra keys even when null. Mixed replies remain
invalid; there is no normalization, budget increase or judgment waiver. The nested
assessment fields remain required, and a fresh bounded Elm-only test evaluates this
protocol correction while preserving the failed profile and original counters.

The subsequent Elm run held after 709.24 seconds when a coverage repair removed
an existing requirement-to-check link. That invariant remains mandatory. Evidence
inspection identified an additional gap: fresh review received probe result IDs and
a plan digest, but not the commands/questions/citations in the executed plan. The
Python probe had successfully imported PyYAML; its version-only output could not
communicate that to an isolated reviewer. Parent-validated reports now retain the
full executed plan after exact source/image/platform/digest/result validation, both
in the durable journal and fresh review/repair evidence. A conflicting embedded
plan is rejected; the parent never adopts model-supplied command authority.

Completed historical probe reports without the executed plan hold before fresh
review and require an explicit restart after confirmed cleanup; missing provenance
is not guessed. Already-ready frozen caches remain governed by their existing
manifest/clean-execution gates. Source/context/journal bounds, probe quotas and the
original deadline do not increase. Repair feedback explicitly lists the required
coverage links, and command definitions may be revised while those links remain.
The obligation-removal guard and independent review/clean-execution gates do not
change. The failed profile and its original counters are preserved.
