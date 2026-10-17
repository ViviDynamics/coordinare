# Feature Specification: Reviewed environment manifest

**Feature Branch**: `351-reviewed-env-manifest`

**Created**: 2026-10-17

**Status**: Draft for maintainer review

**Issue**: [#351](https://github.com/ViviDynamics/coordinare/issues/351)

**Design baseline**: `4bb79e7e` (main, including 218, 219, 234, 265, and 348)

## Intent and confirmed decision

Coordinare must bootstrap projects without embedding knowledge of their programming
language, framework, libraries, package manager, or directory layout in its core.
Project-specific knowledge belongs in an evidence-backed environment manifest derived
from the repository. Installation and verification must use the same accepted manifest.

The maintainer [confirmed the readiness policy](https://github.com/ViviDynamics/coordinare/issues/351#issuecomment-6233595899):
independent agent review of manifest coverage is mandatory, and every required check
must execute successfully in a clean consumer environment. The agent can reject an
incomplete manifest; it cannot waive a failed, missing, timed-out, or unrun check.

This specification proposes the remaining details for review. It does not claim that
agent assessment proves completeness or that successful execution proves installed
binaries are benign.

## Current behavior and scope

The original issue audited `e0729e9d`. Main has since incorporated stronger bootstrap
trust, safe cache writes, frozen content provenance, and transport-specific clean
verification. In particular, an unavailable clean verification now holds completion;
the daemon no longer executes an installer-writable copy of `verify.sh`. Preserve
these improvements. The issue's older failure descriptions are historical evidence,
not a claim that every failure still exists unchanged at this baseline.

The remaining design problem spans `models/env_manifest.py`, `services/env_manifest.py`,
`services/env_manifest_llm.py`, bootstrap payload/persona construction in
`http_performer_service.py`, `env_cache.py`, consumer activation in performer
`workspace.py`, and the service-inference contract. The current manifest still has
Ruby/Node-specific item kinds, a closed filename/parser catalog, runtime-manager
path discovery, framework smoke probes, and category-based optional checks.

This feature replaces those project-stack decisions in the environment path. It
integrates existing service requirements into the shared manifest and removes implicit
selection of service-kind recipes from that path. Explicit platform capabilities may
continue to provide generic facilities such as Debian archive extraction, process
supervision, and container execution. They may not silently choose a project's database,
framework, package, install command, activation path, or verification requirement.

Coordinare's own implementation language, harness installation, security scanners,
non-environment QA capture tools, general workflow routing, and new operating-system
support are outside this change. Issue #349's separately owned gem-context repair is
not taken over. Existing service declarations must migrate without losing required
initialization, health, or shutdown behavior.

## User Scenarios & Testing

### User Story 1: Bootstrap an unfamiliar project (Priority: P1)

An operator connects a project whose ecosystem Coordinare has never seen. A discovery
agent inspects its repository and produces requirements, installation actions,
activation declarations, service lifecycle actions, and executable checks. No core
parser, runtime alias, or framework branch is added.

**Independent Test**: Supply an unfamiliar synthetic toolchain fixture with documented
setup and checks, then bootstrap and verify it through the generic contract.

**Acceptance Scenarios**:

1. **Given** documented setup and a pinned tool constraint in an unfamiliar subproject,
   **when** discovery runs, **then** the manifest cites the evidence, identifies the
   subproject working directory, and contains an executable constraint check.
2. **Given** a repository with conflicting setup instructions, **when** requirements
   cannot be resolved from evidence, **then** discovery records the conflict and the
   environment is held with an actionable question rather than a guessed pass.
3. **Given** a project with no additional dependencies, **when** discovery and independent
   review agree on that conclusion, **then** an explicit empty-environment justification
   and a required baseline usability check permit readiness; an empty parse does not.

### User Story 2: Independently assess coverage (Priority: P1)

Before installation, a fresh-context agent compares the proposed manifest with the
repository evidence and returns `accept`, `revise`, or `indeterminate`.

**Independent Test**: Propose a manifest that omits a documented requirement. The
reviewer identifies the omission with a citation and installation is withheld.

**Acceptance Scenarios**:

1. **Given** an omitted required service, **when** the reviewer inspects the manifest and
   source snapshot, **then** it requests revision and identifies the missing coverage.
2. **Given** malformed, unsupported, truncated, unavailable, or timed-out review output,
   **when** the gate evaluates it, **then** no accepted assessment exists and readiness
   is held. Missing review configuration is an actionable configuration error.
3. **Given** a changed requirement, requiredness, command, activation path, or execution
   context after acceptance, **when** installation or verification is requested,
   **then** the new manifest requires independent acceptance before use.

### User Story 3: Verify the accepted contract in a clean consumer (Priority: P1)

The operator can inspect actual outcomes for every applicable required check. Bootstrap
success prose and coverage acceptance cannot override execution evidence.

**Independent Test**: Accept a manifest whose required command is `false`; execute it
through a clean verifier and confirm that dispatch remains held.

**Acceptance Scenarios**:

1. **Given** a required command returns nonzero, **when** other commands succeed,
   **then** the failed check still blocks readiness without a private shell flag convention.
2. **Given** a tool exists only in the installer container, **when** the cache is mounted
   read-only into a fresh consumer-compatible environment, **then** its required check fails.
3. **Given** a required app check needs repository files and a live service, **when** the
   verifier runs, **then** it uses the declared source snapshot, working directory, and
   disposable service state; it never silently skips a missing prerequisite.
4. **Given** a reviewer says accept but a required check is absent, skipped, timed out,
   or indeterminate, **when** readiness is evaluated, **then** readiness is withheld.
5. **Given** an explicitly optional check fails, **when** all required checks pass,
   **then** the result is visible as optional and does not silently weaken required coverage.

### User Story 4: Reuse only applicable, unchanged environments (Priority: P2)

The operator can reuse a verified cache while changes to environment inputs, repository
structure, manifests, or consumer platforms trigger the appropriate reassessment.

**Independent Test**: Add a dependency file under a new subproject without editing
previously inspected files and confirm that discovery cannot reuse stale coverage.

**Acceptance Scenarios**:

1. **Given** an inspected input is modified or deleted, or a repository path is added,
   **when** the source tree changes, **then** the discovery record is invalidated and the
   changed snapshot is assessed before reuse. New files cannot evade discovery tracking.
2. **Given** the new snapshot yields equivalent environment actions and installed content
   remains applicable, **when** the updated contract is accepted and checks pass,
   **then** the cache may be reused without reinstalling its unchanged dependencies.
3. **Given** cache content changes after verification, **when** dispatch checks provenance,
   **then** existing content-integrity safeguards hold dispatch and request repair.
4. **Given** an old cache or incompatible daemon/performer contract, **when** the upgraded
   system considers it, **then** it holds and schedules bounded reconstruction or reports
   an upgrade requirement; it never infers acceptance from old scripts or success flags.

### Edge Cases

- Monorepos, nested working directories, absent lockfiles, conflicting pins, optional
  dependencies, environment-variable references, and tools with writable runtime state.
- A discovery/read/output limit reached before sufficient repository evidence is available.
- Installer attempts to alter accepted commands, remove checks, downgrade requiredness,
  replace activation, or mutate content while clean verification is running.
- A check prints misleading success text, closes output without reporting status, spawns
  children, consumes stdin, times out, or has a prerequisite that failed.
- Required services fail initialization or health, need unavailable external access, or
  cannot run under the consumer platform's declared privileges.
- Private repositories requiring source material in verification but no GitHub credentials
  in the execution container; source material is supplied before untrusted checks run.
- Concurrent bootstraps or consumers, daemon restarts, wiped claims, image changes, and
  interrupted publication of accepted manifests or verification evidence.

## Requirements

### Functional Requirements

- **FR-001: Generic core.** The environment core MUST operate on validated declarations
  and explicit commands. It MUST NOT select behavior by language, framework, library,
  package name, conventional dependency filename, or version-manager directory layout.
  Such names may appear as project evidence or fixture data, not executable core policy.
- **FR-002: Discovery evidence.** Discovery MUST inspect a bounded immutable repository
  snapshot, including relevant documentation, build/test entry points, automation, and
  subprojects. It MUST record the snapshot identity, paths read, content identities,
  source citations, inventory scope, and any incomplete discovery. It MUST support
  arbitrary filenames rather than relying on a closed ecosystem catalog.
- **FR-003: Strict manifest.** One versioned manifest MUST identify the project and source
  snapshot, requirements, action/check IDs, dependencies, activation, service lifecycle,
  applicability, and evidence. Structural validation MUST reject unknown schema versions,
  duplicate IDs, missing references, cyclic action dependencies, invalid paths, missing
  required checks, and unsupported execution capabilities before installation.
- **FR-004: Explicit semantics.** Every action/check MUST declare its command, arguments
  or explicit shell interpreter, working directory, environment references, execution
  phase/context, prerequisites, and timeout. Checks MUST declare requiredness and scope.
  Shell commands MUST be isolated from the runner's own control flow. The runtime MUST
  record command exit status; it MUST NOT infer success by parsing success prose.
- **FR-005: Constraint coverage.** Each required requirement, including version/range,
  loadability, or service constraints, MUST map to one or more required executable checks.
  The reviewer MUST assess whether these checks exercise the stated constraints. The
  core MUST NOT invent ecosystem-specific version comparisons or presence-only defaults.
- **FR-006: Activation.** Required activation paths and environment updates MUST be
  declared explicitly with their cache/source/scratch roots. Trusted runtime code MUST
  render/apply them deterministically without searching runtime-manager layouts or
  executing installer-written activation text. Activation MUST be safe to repeat and
  MUST NOT start services implicitly. Missing required paths fail a check.
- **FR-007: Services.** Service initialization, start, health, and stop actions, their
  ordering, and persistent-versus-disposable state requirements MUST be explicit in the
  manifest. Required service readiness MUST be checked in the consuming context with
  writable state outside the read-only cache. Existing service declarations MUST be
  converted into reviewable manifest data; no separate service result can waive the
  shared gate. Automatically selected built-in service-kind recipes MUST be removed
  from this path. Unsupported privilege/platform needs yield an explicit hold.
- **FR-008: Independent review.** A fresh-context reviewer MUST have read-only access to
  the proposed manifest and the same repository snapshot, and be able to inspect more
  evidence within bounds. It MUST return a schema-validated assessment bound to the
  manifest digest and source identity, with cited objections or acceptance rationale.
  Fresh context means a separate invocation without installer/discoverer conversation
  state, writable cache access, or tools that mutate the contract. It need not use a
  different model vendor. Silence or exhausted inspection is not acceptance.
- **FR-009: Accepted identity.** Trusted orchestration MUST validate and persist the
  accepted manifest outside installer-writable mounts. Its digest MUST cover all
  execution-affecting fields and source identities using a canonical encoding.
  Installation, activation, checks, and assessments MUST reference that digest.
  The cache may hold an informational copy, never the authoritative contract.
- **FR-010: Revisions and repairs.** Repairs that preserve the accepted contract may
  repair installed content and rerun checks within existing budgets. Any contract
  change MUST create a new digest and obtain independent acceptance. An installer
  cannot amend or approve its own requirements, checks, applicability, or optionality.
- **FR-011: Clean verification.** Required checks MUST execute in a fresh, isolated
  consumer-compatible environment on the configured transport. It MUST use the same
  absolute cache path, a read-only cache, explicitly supplied source inputs, disposable
  writable state, and declared platform capabilities. Disable automatic cache profiles
  and run only accepted activation/actions. Installation-only network access, credentials,
  processes, and writable state MUST NOT leak into verification. Check execution has
  no network by default; declared external-service access requires operator policy.
- **FR-012: Transport and trust continuity.** Docker and Kubernetes verification MUST
  preserve writer-quiescence confirmation, anchored safe writes, provenance checks,
  isolated trusted result collection, bounded logs, and teardown from specs 218, 219,
  234, and 265. Do not replace transport-specific verification with a daemon checkout
  or require Docker on a Kubernetes daemon. Transport routing MUST honor 348.
- **FR-013: Execution evidence.** Trusted runtime code MUST produce per-check outcomes
  (`pass`, `fail`, `indeterminate`, or `not_run`) with check ID, manifest digest,
  environment/content identity, context, exit status when available, duration, and
  redacted bounded output. A failed prerequisite prevents execution and is recorded;
  missing evidence is never synthesized as a pass. Untrusted process output MUST NOT
  be able to forge trusted result records or change the runner's final status.
- **FR-014: Readiness.** Readiness requires a matching accepted coverage assessment,
  valid frozen cache provenance, compatible execution identity, and `pass` for every
  required check applicable to the requested consumer profile/stage. Optional outcomes
  remain visible. Any failed required check yields failure; any missing/unrun/unknown
  required result holds. No agent, fallback path, success flag, or legacy script may
  waive these requirements. This gate applies to both prose and workflow bootstrap.
- **FR-015: Empty environments.** A manifest with no additional project dependencies
  MUST explicitly justify that conclusion and still declare a required executable
  check of the project's documented baseline usage. It requires independent review.
  Discovery failure and empty parsing MUST NOT be treated as an empty environment.
- **FR-016: Cache identity.** The readiness record MUST bind the accepted manifest,
  installed-content provenance, source inputs, consumer/verifier platform and immutable
  image identity, schema and generation/review policy revisions, and operator execution
  policy. Dynamic results and post-install content hashes remain separate records bound
  to the manifest digest, avoiding a self-referential manifest hash. Different consumer
  profiles require their own applicable required-check evidence.
- **FR-017: Rediscovery.** Any change to the tracked source tree inventory or inspected
  input content MUST invalidate discovery for that snapshot, including new/deleted
  files and new subprojects. Rediscovery and acceptance may permit reuse of unchanged
  installations, followed by applicable clean checks. Changes to execution inputs or
  policies invalidate affected acceptance/evidence. Polling an unchanged identity MUST
  not invoke an agent or rebuild the environment.
- **FR-018: Lifecycle and publication.** Publish readiness only after accepted checks
  and frozen provenance are complete for the same attempt. Serialize competing writers
  and atomically publish matching records. Persist attempts and budgets across restarts.
  Stale completions/results from another digest or attempt MUST NOT overwrite readiness.
  Cache mutation invalidates execution evidence and requires re-verification.
- **FR-019: Bounded work.** Discovery, review, installation repair, checks, and cleanup
  MUST have explicit budgets. Preserve existing outer verdict/infrastructure attempt
  accounting and the bounded workflow repair budget. Default to one discovery and one
  review invocation per candidate; allow at most two reviewed contract revisions within
  an outer attempt. Exhaustion holds with the exact cause; it cannot retry each poll.
- **FR-020: Secrets and authority.** Manifest/assessment/evidence persistence MUST store
  secret variable names rather than literal values. Redact execution output before
  persistence or publication. Review tools remain read-only. Execution runs within the
  existing performer privilege boundary, with no daemon credentials and no silent
  privilege escalation. Supported environment references MUST have defined resolution
  and quoting, rather than unrestricted text substitution in trusted wrapper code.
- **FR-021: Migration.** Old caches without the accepted contract MUST hold and rebuild
  or be adopted only through new discovery, independent acceptance, clean execution,
  and provenance verification. Mixed-version daemon/performer pairs MUST hold with an
  actionable upgrade reason. Do not delete existing cache data before replacement is
  available. An operator can disable environment caching, but cannot disable review
  while presenting a cache as verified under this contract.
- **FR-022: Operator experience.** Existing dashboard/status channels MUST show the
  current phase, held reason, manifest identity, assessment rationale, check outcomes,
  and exhausted budget. Never show ready while required evidence is pending. Update
  onboarding and deployment guidance, including required review-role configuration.
- **FR-023: Governance.** Propose an explicit language/framework/library neutrality
  principle as a separately reviewable constitutional amendment, with the required
  version/date and dependent-template review. This draft does not amend the constitution
  or weaken existing safety, test, provenance, or review rules.

### Key Entities

- **Environment manifest**: immutable accepted requirements and execution contract;
  stable item/action/check IDs, project evidence, source identity, and digest.
- **Coverage assessment**: fresh-context reviewer verdict, rationale and citations,
  manifest/source identities, review policy revision, and bounded inspection record.
- **Execution context**: consumer profile/stage, image/platform capabilities, mount
  roots, source material, scratch state, and permitted external access.
- **Verification record**: trusted per-check evidence and aggregate decision for one
  manifest, execution context, content identity, and attempt.
- **Readiness record**: atomic reference to matching acceptance, successful required
  verification, and frozen cache provenance; distinct from installer self-report.

## Success Criteria

### Measurable Outcomes

- **SC-001**: Ruby/Rails, at least two previously unsupported ecosystems, a monorepo,
  and an unfamiliar synthetic toolchain bootstrap using one generic runner without
  adding stack-specific executable core branches. Ecosystem names occur only in
  evidence/fixtures and explicitly scoped compatibility migration data.
- **SC-002**: Deterministic fixture tests cover all acceptance scenarios and edge-case
  classes. Every missing, failed, timed-out, malformed, stale, or unrun required result
  withholds readiness, including `false`, incorrect versions, missing binaries, and
  service-health failures. Optional checks never downgrade required requirements.
- **SC-003**: Contract/integration tests demonstrate the same digest throughout review,
  installation, activation, and clean verification on Docker and Kubernetes, including
  tampered contract copies, forged output, mixed versions, interrupted publication,
  cancellation, and restart. Existing trust/provenance regression suites remain passing.
- **SC-004**: No agent calls occur on an unchanged-identity poll or routine verified
  consumer dispatch. Changed snapshot inventory triggers rediscovery even when all
  previously read files are unchanged; equivalent requirements can reuse installed bytes.
- **SC-005**: Default discovery and review deadlines are each 120 seconds; the existing
  positive outer bootstrap deadline also applies, and the shorter remaining budget wins.
  A check defaults to 180 seconds and cannot exceed the outer remaining budget. Cleanup
  has a 30-second grace budget and must either confirm termination or hold further work.
  These are configurable limits, not promises about model response time.
- **SC-006**: Canonicalization, validation, and readiness aggregation for a fixture with
  1,000 requirements and 1,000 checks complete within 250 ms p95 over 100 warm iterations
  on a documented CI runner, excluding agent calls, subprocesses, network, and existing
  provenance walks. The benchmark uses compact records within the serialization cap.
  Serialized manifest and each assessment are capped at 256 KiB;
  over-limit or truncated evidence holds rather than accepting partial coverage. Record
  the measurement method and runner in the implementation plan; record measured timings,
  hardware, and effects on existing provenance/transport payload bounds in the validation report.
- **SC-007**: Verification adds no model calls beyond coverage review. Its cost scales
  with declared applicable checks; output tails retain at most 4 KiB per check and
  256 KiB per attempt, with truncation recorded. Trusted result metadata is retained
  independently of output truncation. No secret values appear in persisted artifacts.
- **SC-008**: Discovery and review each default to at most 256 file reads, 256 KiB
  per file, 4 MiB of aggregate source text, and 100,000 inventory entries. Limits are
  configurable, and exhaustion must be explicit. An incomplete inventory or truncated
  source cannot establish coverage unless the reviewer obtains the missing evidence
  within the configured limits. Input-size and command-count admission limits must
  reject oversized work before scheduling it.
- **SC-009**: Before merge, lint, typecheck, affected unit/integration/contract tests,
  full required checks, and `make test-all` pass. Full-diff adversarial review verifies
  the readiness and trust claims by execution. Operator documentation describes the
  new contract, review configuration, budgets, migration, and recovery actions.

## Delivery and review boundaries

This draft is the specification for one environment-contract replacement, not permission
to implement it. After maintainer review, produce `plan.md`, `tasks.md`, `data-model.md`,
and `contracts/` here, run `speckit.analyze`, and resolve findings before implementation.
The plan must map issue #351's audit findings to changed/retained paths, account for
intervening fixes, and identify separately reviewable governance and migration changes.

Proposed implementation slices are the generic contract/runner, discovery and coverage
review, bootstrap/consumer integration, and service/legacy migration. Partial slices
must not advertise the new verified-ready state before its full gate exists. Deployment
must coordinate daemon and performer capability versions; old readiness cannot silently
satisfy the new contract.
