> Maintainer-approved source conflict correction: [source-conflict-amendment.md](source-conflict-amendment.md). Inline TDD plan: [source-conflict-plan.md](source-conflict-plan.md).

> Amendment: [repair-amendment.md](repair-amendment.md) supersedes proposal provenance ownership and extends the shared repair budget through executable failures. Implementation: [repair-plan.md](repair-plan.md). User explicitly authorized implementation and retesting.

# Feature Specification: Practical reviewed environment discovery

**Feature Branch**: `353-practical-env-discovery`
**Created**: 2026-10-17
**Status**: Approved by maintainer on 2026-10-17
**Issue**: https://github.com/ViviDynamics/coordinare/issues/353
**Input**: Fix the observed discovery and source-transfer blockers from PR #352,
then repeat environment bootstrap validation against Elm and eligible Vivi repositories.

## Intent and evidence

Coordinare must derive environment requirements from arbitrary repositories, obtain
independent coverage approval, and execute every required check in a clean consumer.
The user approved the correction direction: practical evidence navigation, explicit
execution guidance, bounded repair, consistent source transfer, and real-repository
retesting. This draft records the details proposed for implementation.

The baseline tested 29 active non-Rails-primary repositories at pinned commits:
28 discovery deadlines and one unsupported tool response, with no independent review,
installation, clean verification or readiness reached. Seven additional completed
profiles also held. This is evidence about the tested model/configuration, not all
providers. Private baseline artifacts are in `.agents/state/351-org-validation/`.

Elm at `bc224a78db6f669d56863599d653f14e7605de0b` held at 120 and 600 seconds.
Its first 17 inspected files serialize to 315,664 bytes, exceeding the verifier's
262,144-byte limit. Discovery permits 4 MiB. The initial inventory alone is 356,239
bytes. Simply extending deadlines or changing models does not fix these product gaps.

## Scope and selected design

Keep the existing manifest and readiness architecture. Improve the interfaces supplying
source evidence and execution capabilities, plus the bounded model conversation.
Do not add ecosystem catalogs, install recipes, alternative readiness rules, or a new
agent framework. Issue #349 remains separate. No constitution change is proposed.

Increasing timeouts alone leaves transport and schema defects unresolved. Splitting
inspected evidence from executable inputs would change provenance semantics. Instead,
retain every inspected input and transfer its pinned contents through bounded payloads
outside pod environment variables, with complete reconstruction checks.

## User Scenarios & Testing

### User Story 1 - Discover an unfamiliar nested project (Priority: P1)

An operator can bootstrap a polyglot or unfamiliar repository without core logic that
recognizes particular languages, frameworks, filenames or package managers.

**Why this priority**: Every baseline repository held before an accepted manifest.

**Independent Test**: A deterministic model double navigates a large nested inventory,
reads evidence in batches, and supplies a valid manifest and independently read review.

**Acceptance Scenarios**:

1. Given 100,000 admitted paths, the initial model prompt contains bounded root
   navigation and identity, not the full listing. All paths remain discoverable via
   deterministic pagination, including unfamiliar toolchains and nested projects.
2. Given multiple requested files, a bounded read batch returns exact pinned content
   and hashes. Duplicate reads cannot evade limits or replace changed evidence.
3. Given an accepted manifest, every file inspected by discovery is retained in its
   input hash map. The independent reviewer reads every manifest input and can browse
   further evidence without inheriting the discovery conversation.

### User Story 2 - Correct a malformed proposal safely (Priority: P1)

An agent receives enough runtime information and validation feedback to construct a
usable environment contract without requiring an operator to edit generated manifests.

**Why this priority**: Actual small-project candidates used invalid check references
or install-only dependencies; validation currently terminates without feedback.

**Independent Test**: Invalid proposal, sanitized correction feedback, valid revision,
fresh coverage approval; then a deliberately failing required command still holds.

**Acceptance Scenarios**:

1. Both model phases receive the actual immutable target image and platform,
   persistent-cache semantics, disposable source workspace, writable scratch,
   installation/verification network policies, root substitutions, credential reference
   rules, subprocess boundaries, and check-ID/dependency rules. Missing capabilities
   hold rather than supplying invented defaults.
2. Invalid check references or install-only dependencies receive bounded field paths
   and trusted validation codes. Candidate values and arbitrary exception text are not
   echoed into persisted diagnostics. The next proposal undergoes full validation.
3. A review requesting revision yields bounded cited objections to discovery. Every
   resulting candidate requires a new independent conversation and new digest binding.
   A review rejecting coverage terminates the attempt.
4. Exhausted repair/time/call budgets, invalid identity, unavailable evidence, unsupported
   execution capabilities and failed/unrun checks all hold. Repair cannot manufacture
   input hashes, remove required obligations to pass validation, or override execution.

### User Story 3 - Verify a legitimate large evidence bundle (Priority: P1)

An operator can verify a manifest whose inspected evidence fits discovery's advertised
limits without encountering a smaller hidden verifier transport cap.

**Why this priority**: The observed Elm evidence exceeds the current transport limit.

**Independent Test**: Reconstruct an exact source bundle exceeding 315,664 serialized
bytes on Docker and Kubernetes adapters; mutate, omit and duplicate chunks to prove holds.

**Acceptance Scenarios**:

1. All supported adapters admit the same canonical source document, bounded at 16 MiB
   serialized UTF-8 JSON, with discovery's raw text allowance defaulting to 4 MiB.
   If serialization overhead exhausts the document bound, hold before coverage approval.
2. Docker sends source through trusted stdin or mounted private payload files.
   Kubernetes mounts bounded immutable Secret chunks in the verifier; no source bundle
   is embedded in pod environment variables or image build context. Every individual
   API object remains below the existing 900 KiB safety bound.
3. Reconstruction checks declared chunk sequence/count, total size, document digest,
   exact relative paths and every manifest input hash before any project command.
   No extraction traversal, symlink substitution, extra file or partial bundle is accepted.
4. Payloads are readable only by trusted verifier setup, contain no injected runtime
   credential values, and are cleaned after success, failure, cancellation or restart
   reconciliation. Creation failures clean partial resources and hold verification.

### Edge Cases

- Multibyte text, JSON escaping expansion, long names, empty directories and paginated
  inventories must obey byte bounds with deterministic ordering.
- Added/deleted paths change the inventory identity even if previously read contents
  match. Source commit, image/platform or execution-capability changes invalidate reuse.
- Malformed JSON and unsupported tool envelopes can receive protocol feedback only
  within the same repair budget; source drift and integrity failures terminate immediately.
- Missing credentials or external prerequisites are explicit holds, never omitted checks.
- Documentation-only repositories require an evidence-backed baseline usage check;
  an empty manifest cannot become readiness without executable evidence.

## Requirements

### Functional Requirements

- **FR-001 Neutral evidence access**: Provide generic paginated directory browsing and
  batched text reads. Default listing pages contain at most 200 entries and 32 KiB;
  batches contain at most 8 files. Existing per-file/read/source bounds still apply.
  Keep a complete pinned inventory and its digest outside model context.
- **FR-002 Execution guidance**: Pass a versioned execution-capability description from
  actual runtime configuration to discovery and review. Explain `{repo}`, `{cache}`,
  `{scratch}`, `${NAME}`, argv execution, explicit shell use, and required check IDs
  in schema descriptions and prompts. Bind its digest into accepted review/cache identity;
  legacy records missing that binding require rediscovery. Do not reinterpret old evidence.
- **FR-003 Bounded repair**: At most two repair rounds across malformed proposals,
  protocol violations and coverage revision requests per attempt. No budget reset when
  starting a fresh review. Default total attempt deadline is 600 seconds, configurable
  up to 3600 seconds. Existing 120-second per-phase setting remains configurable and
  cannot extend the aggregate deadline. Default aggregate model-call limit is 128;
  aggregate file-read limit is 512. Cancellation terminates outstanding work.
- **FR-004 Evidence integrity**: Keep all actual inspected source hashes and complete
  inventory identity. Never summarize away an input or substitute source contents to
  satisfy transport. Review extra reads remain bound to its assessment as today.
- **FR-005 Bounded source transfer**: Share document serialization/admission/reconstruction
  across adapters. Enforce 16 MiB serialized document maximum, existing per-file bounds,
  64 KiB decoded chunk maximum and at most 256 chunks. Use opaque resource names and
  ownership labels; never log source payloads. Remove the hidden 256 KiB bundle ceiling.
- **FR-006 Readiness invariant**: Fresh independent coverage approval and deterministic
  successful execution of every required check in a clean consumer are mandatory.
  Installer and consumer use the same accepted manifest digest, immutable image and
  platform identity. Failed, missing, unknown, skipped or unrun required evidence holds.
- **FR-007 Diagnostics**: Record stage, elapsed time, calls, inspected bytes, context
  bytes, repairs, limit exhausted and safe validation codes. Record provider token usage
  and finish reason when available; mark unavailable metadata explicitly. Do not infer
  token exhaustion from a timeout or persist source/credential-bearing exception text.
- **FR-008 Focused change**: Preserve current generic activation/service lifecycle,
  secret references/redaction, provenance checks and old-cache fail-closed migration.
  No framework-specific core logic or unrelated refactors.

### Key Entities

- **Execution capabilities**: Versioned immutable image/platform and runtime constraints
  supplied to both phases and bound to acceptance/cache identity.
- **Inspection ledger**: Complete pinned inventory identity, exact file hashes and bytes,
  with per-phase independent evidence and aggregate resource accounting.
- **Attempt budget**: One deadline and finite call/read/repair counters shared across phases.
- **Source payload**: Canonical bounded document plus ordered chunk metadata and digest;
  transport resources have attempt ownership and cleanup lifecycle.

## Success Criteria

### Measurable Outcomes

- **SC-001**: Deterministic regressions reproduce each observed failure shape and pass
  with the correction, including large inventory, source transfer above 256 KiB,
  malformed references, install-only dependencies and exhausted repairs.
- **SC-002**: A required command that fails, times out or never runs cannot produce ready
  evidence despite valid manifests and approving reviews. Both adapters retain this behavior.
- **SC-003**: Retest pinned `conductor-e2e-sample`, then Elm, followed by all 29 baseline
  eligible repositories. Report discovery, coverage, installation, clean verification
  and readiness separately. Compare the same baseline model profile with any separately
  identified alternatives; do not replace failed baseline results with tuned outcomes.
- **SC-004**: Demonstrate genuine independent acceptance and passing installer/clean-consumer
  checks on the small sample and Elm before claiming these corrections solve bootstrap.
  If either holds, investigate and report the exact stage; do not fabricate approvals or
  declare the real-repository validation complete as a success.
- **SC-005**: Full regression suite runs through CI, with focused local TDD as needed.
  All original checkout changes and separate #349 work remain untouched.
- **SC-006**: Live validation uses disposable environments and pinned sources, executes
  no deployment/release/hardware operations, and exposes no secrets or private transcripts.

## Delivery and validation boundaries

Implement against fresh main in this existing isolated workspace, with a feature branch
matching this spec. Run `speckit.analyze` before implementation. Add targeted regression
fixtures with synthetic contents; retain private repository source/transcripts outside
tracked files. Adversarial full-diff review and the configured PR review/CI gates apply.

The organization test harness must invoke the real discovery/review and installer/clean
verifier entrypoints, supply real runtime capabilities, and inspect proposed commands
for environment-validation scope. A held repository is a recorded result, not a passing
bootstrap. Full daemon/outer-retry behavior must be tested separately from direct service
entrypoint tests and identified explicitly in the report.
