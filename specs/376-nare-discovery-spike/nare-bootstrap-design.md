# Nare bootstrap runtime integration

Date: 2026-11-14
Status: design for approval; no production implementation authorized by this document yet

## Intent and decisions

Replace Coordinare's bespoke bootstrap model conversation loops with nare, then
assess other internal agent tasks for the same runtime. The maintainer selected
migration of the bootstrap agent loops with removal of the old implementation
once the replacement passes. There will be no permanent selectable legacy chat
runtime and no automatic fallback to it after a nare failure.

This amends the 353 specification's earlier prohibition on introducing an agent
framework for this narrowly defined runtime boundary. It does not supersede its
source, scope, review, execution, or readiness rules. The upstream dependency
is ViviDynamics/nare, not a new harness implemented inside Coordinare.

## Evidence and limits

The completed saved-packet review used nare revision
`21fd8e7fc010137dd948aef717f16eabb7f359f9` and the existing configured GLM route.
It delivered all 232 immutable evidence pages, including 148 pinned source files,
and returned an accept assessment. Coordinare's real assessment validator passed
both in the run and when replayed independently from the saved session. The
manifest digest remained unchanged. An early answer was refused; native evidence
reads then resumed. Three elisions and a checkpoint continuation completed with
no connection retries. Active runtime was approximately 22 minutes, with
8,117,496 cumulative tokens. The continuation used 758,332 additional tokens.

Nine isolated tests cover full page bytes at the actual model-input boundary,
source and manifest tampering, validator integration, native-tool correction,
transport-independent assessment criteria, timeout reporting, and checkpoint
integrity. Private transcripts and source remain gitignored. A sanitized outcome
is recorded in `.agents/state/353-nare-review/nare-review-outcome.json` in this
worktree. It must not become a runtime dependency or a committed source fixture.

This proves one review path, not authoring reliability, lower cost, a completed
bootstrap, or deployment readiness. Earlier authoring experiments still showed
repair convergence and output-volume problems. No cache-read tokens were reported.
Production acceptance must exercise new authoring, execution repair, the real
installer, clean-consumer verification, and provenance gates.

## Approaches

1. **Reviewer-only adapter.** Smallest initial change, but leaves separate scope,
   authoring, correction, and repair conversation implementations. Useful as the
   first delivery slice, not the target architecture.
2. **Shared nare runtime for bootstrap phases, with Coordinare-owned domain
   tools and validators. Selected.** Preserves existing authority boundaries,
   removes duplicated harness behavior, and provides a reusable foundation
   without designing a universal workflow framework first.
3. **Move bootstrap orchestration and execution into an autonomous nare agent.**
   Rejected: this would combine model conversation management with approval,
   cleanup, readiness, and retry authority, unnecessarily changing domain rules.

Migration is staged for verification, not maintained as two production engines.
The final cutover removes old loops and runtime selection switches.

## Ownership boundary

Nare owns the per-agent conversation: native tool dispatch, model interaction,
context elision or supported compaction, structured-answer correction, transient
transport retries, session serialization, and per-session usage reporting.

Coordinare owns phase scheduling, independent author/reviewer separation,
source and inventory identity, requirement and finding ledgers, obligation
preservation, candidate admission, scoped versus full repair, probe/trial
admission, execution authorization, execution cleanup, outer attempt budgets,
clean-consumer validation, and publication/readiness decisions.

A model acceptance is a proposal or assessment. It never marks the bootstrap
ready. Nare does not gain arbitrary shell, filesystem write, network, Docker,
Git, or publication capabilities through this integration.

## Runtime adapter and phases

Introduce a small internal agent-runtime interface, initially implemented only
by nare. A request contains phase and role identity, resolved model configuration,
a compact task description, immutable evidence handles, a final-answer schema,
explicit allowed domain tools, an admission callback, and budget/cancellation
bindings. A result contains the typed validated answer or a typed hold/failure,
usage, evidence references, and a checkpoint reference. Nare session objects stay
inside the adapter rather than spreading through bootstrap state models.

Migrate the existing loops in `environment_discovery._phase`,
`environment_scope._phase`, and `environment_shard_scope._converse` plus their
model-call/correction helpers where those are exclusively loop machinery.
Preserve their domain operations behind callbacks. Map scope author and reviewer,
manifest/global/group author and reviewer, and repair author and reviewer onto
separate sessions. Never reuse an author's conversation as the independent
reviewer's context. Reuse a session only within the same role/phase identity and
existing attempt rules.

The source/inventory, evidence-reader, probe, and trial adapters call the current
Coordinare implementations. Probes and trials require existing policy validation,
identity checks, budgets, authorization, and cleanup. A trial remains diagnostic
only and cannot produce consumer or runtime-artifact evidence. A pending external
operation is reconciled before any session can replay a tool call. A model retry
must not repeat an already executed probe, trial, or installation.

Keep the current envelope schemas and domain validators. Separate transport
instructions from review/authoring criteria as explicit builders; do not ship the
spike's string-splitting replacement of a legacy prompt. Native tool schemas are
not alternatives in the final-answer schema. Use nare's check callback for
correctable domain refusals and preserve named errors and existing correction
accounting. Do not silently relax schemas to accommodate the runtime.

## Evidence delivery and compaction

Large inventories, ledgers, prior execution output, and source text are immutable,
addressable tool evidence, not one enormous initial user prompt. Initial context
contains the task, identity, policy and an evidence catalog. Pages have stable
artifact identity, offsets, and content digests; use synthetic fixtures in tests.

Serialize tool envelopes before returning them. Bound the serialized result below
the runtime output cap, return whole pages only, and provide an explicit next
cursor/unread indicator. Mark coverage only for pages actually delivered. Oversize
single records must be repaginated or refused, never silently cut. Redacted event
streams are diagnostics, not proof that exact source bytes reached the model.

Required evidence depends on the existing phase contract. A global full-evidence
review cannot skip required pages; a scoped delta review must retain its existing
scope and evidence semantics rather than receiving every repository file merely
because the spike did. Authors retain source navigation and citation requirements.
Inspection records and source hashes are re-derived by Coordinare from the pinned
source provider. No new daemon-side repository clone is introduced.

Compaction can discard transcript details, not the canonical evidence store,
coverage ledger, open findings, original constraints, or remaining budget. The
agent can reread immutable pages. Record compaction events from the emitted stream,
not a session queue that the runtime drains. Delivery is necessary, but is not
proof of semantic understanding; independent review and execution remain required.

## Persistence, accounting, and cancellation

Store versioned session checkpoints plus a Coordinare-owned receipt binding the
session to phase, role, source commit, inventory/scope/capability/manifest digests
as applicable, policy/tool contract, model configuration, prompt/schema revisions,
evidence coverage, prior usage, and outer attempt identity. Checkpoint hashes
alone do not establish authority: verify the receipt against current domain state.
Use atomic writes and durable receipts at completed tool/turn boundaries. Secrets
are resolved at runtime; never put credentials in checkpoint metadata or logs.

On resume, verify identity and artifact integrity before restoring coverage.
Revalidate pinned source hashes. Preserve token use, turn counts, correction
counts, phase counters, and remaining outer budget. Do not reset the allowance
by loading a session or beginning a repair. Any explicit additional allowance is
recorded separately and must not override the existing outer attempt policy.

The outer Coordinare attempt deadline and repair rules remain authoritative.
Each phase gets the remaining allowance; every model request/retry and domain
tool invocation participates in that accounting. Avoid double-counting usage and
calls across resumed event streams by stable session/turn/event identity. Define
which layer owns each counter in the implementation plan. Persist consumed
allowances even when the answer is refused or a transport request fails; unknown
provider token use remains explicitly unknown, never invented.

Cancellation closes in-flight model streams, stops new tool dispatch, flushes
available usage/checkpoint state, and reports terminal timeout/cancel/error in the
outer result. A saved internal `working` session does not mean a live process or
a completed phase. No timeout, budget exhaustion, missing receipt, or cleanup
uncertainty is converted into an accepted assessment or readiness.

## Model configuration and observability

Keep Coordinare's endpoint/model/mode resolution as the authority. Do not hardcode
the experimental GLM route, gateway URL, local paths, or personal credentials.
The adapter binds the selected endpoint to nare's supported transport with the
configured output/context limits and compatible parameters. Unsupported endpoint
or orchestration combinations fail explicitly before spending model tokens; they
never silently change model, provider, or strategy. Exact supported combinations
and dependency packaging are an implementation preflight deliverable.

Pin a released nare version containing the validated APIs and fixes, or an exact
commit if a suitable release does not exist. Test that exact dependency in the
normal install/build images; never depend on a sibling source checkout or private
Pillow target. No vendored copy of nare's loop or patched site-packages.

Bridge nare events into existing bootstrap activity, conductor transcripts, and
token accounting. Surface phase/role, last progress, delivered/required evidence,
compaction, correction reason, remaining budget, resume provenance, and terminal
hold reason. Deduplicate persisted usage across restart. Keep raw private source
and secrets out of ordinary logs. No promise of caching or cost reduction.

## Existing rulings preserved

The 353 repair amendments remain authoritative except the explicitly replaced
chat-loop mechanism. In particular: frozen obligations, patch-only delta repair,
scoped independent review, source/capability invalidation, separate scope and
executable revision counters, pending-probe recovery, per-execution authorization,
fresh-generation execution and clean-consumer checks remain in force.

The existing delta-to-full domain repair fallback is preserved, with both paths
using nare. It is not a fallback to the legacy chat runtime. Diagnostic trial-disk
reuse remains limited to the approved repair-trial amendment; a failed generation
never becomes a consumer artifact through a checkpoint or model assertion.

## Delivery and deletion gates

1. Promote the reviewer experiment into the production-shaped adapter with native
   prompts/schemas, immutable evidence tools, persistent receipts, configuration
   binding, and telemetry. Replay representative saved cases with synthetic tests
   and at least one bounded live review.
2. Migrate scope and manifest authoring, sharded roles, and repair to that adapter.
   Preserve domain admission, correction, and scheduling contracts; exercise
   malformed proposals and obligation-preserving patches. No declaration of
   authoring success based solely on the saved-packet review.
3. Exercise real bootstrap entrypoints: fresh discovery, independent review,
   authorized probes/trials where applicable, installation, an execution failure
   and repair, full verification, then a separate clean consumer and provenance
   checks. Include daemon restart/cancellation and ensure external work is not
   duplicated. A direct service run does not substitute for daemon lifecycle tests.
4. Remove the old chat loops, manual JSON-tool protocol, redundant context trimming,
   and bootstrap-only transport/correction helpers once their replacements pass.
   Retain shared utilities still used elsewhere. Assert the normal bootstrap path
   cannot instantiate or fall back to a legacy loop. Remove dead switches and tests
   for the retired engine; preserve domain behavior tests and refusal cases.
5. Re-run the acceptance suite on the final deletion diff, then adversarial review,
   required lint/test/build checks and normal PR gates. Do not merge a dual-engine
   interim state as the completed integration.

Passing means the new runtime preserves correctness on a representative corpus,
finishes the real clean-consumer/provenance path, survives checkpoint resume without
relaxed limits or lost evidence, refuses incomplete/tampered evidence and unsafe
execution, and passes regression tests after deletion. Report live cost and time
as measurements, not blanket efficiency claims.

## Other bespoke work, after bootstrap

Survey actual call paths before migrating anything else. First candidate: service
inference's explicit iteration/tool/usage loop. Then examine the configuration
assistant and observer for genuine multi-step tool work and repeated correction
logic. Simple one-shot persona/feedback classifiers need not become agents.

Reuse the minimal runtime interface, model resolution, events, and checkpoint
mechanism. Each task supplies its own allowed tools and validators. Produce a
ranked follow-up backlog with concrete duplicate code, benefit, migration risk,
and acceptance tests. Those later migrations are separate scoped work, not bundled
into bootstrap acceptance or a general framework rewrite.

## Builder choices and implementation prerequisites

Internal module names, serialization mechanics, small helper factoring, and test
fixture layout are implementation choices. Keep policy semantics above fixed.
Use the validated 353 implementation as the functional baseline; the spike branch
is not a production base. Before creating the implementation branch, resolve the
current 353 PR/main relationship and select a base containing the required work,
without discarding concurrent changes or moving the frozen validation checkout.

The implementation plan must inventory every old loop and its callers; map each
existing budget and admission callback; choose and verify the nare dependency;
record the supported endpoint/mode matrix; define checkpoint compatibility and
migration of pre-nare in-flight attempts; recover any additional applicable 353
rulings; and run `speckit.analyze` before implementation. Legacy sessions must never
be reinterpreted as nare evidence. If compatibility cannot be established, hold
and reconcile external work before a fresh authorized phase under the remaining
attempt budget. The final spec/plan must document that policy concretely.

This document is committed in the existing spike worktree under the repository's
numbered-spec convention. Production files and the validated 353 checkout remain
unchanged while the design is reviewed.
