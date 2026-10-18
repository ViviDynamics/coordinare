# Coverage before executable manifest construction

Issue: #353. PR: #354. Status: proposed, awaiting written-spec approval.

## Problem demonstrated by the tests

The existing loop asks one model response to discover scope, choose prerequisites,
write installation commands, establish activation and produce clean checks. Its
independent reviewer then assesses that candidate. This permits candidate omissions
and image limitations to influence what the reviewer considers required. Repairing
the JSON interface has improved structural recovery without proving whole-project
coverage or executable readiness.

The latest real Elm run at Coordinare `41fc7722` stopped after 650.6 seconds, not at
its 3,600-second deadline. It used eight model calls and exhausted the original two
schema repairs. A complete declaration successfully repaired install-only check
dependencies; the reviewer then needed a correction for a missing verdict. Following
four coverage findings, construction linked a required environment requirement to
an optional runtime check. The validator correctly rejected that relationship.
Installation and clean verification never ran. The reviewer also still treated
client-only container checks as acceptable for part of the documented environment.

The pinned Elm source remains `bc224a78db6f669d56863599d653f14e7605de0b`.
Private evidence: `complete-correction-elm60m-low/elm/result.json`, responses
`chat-003.json` through `chat-008.json`, and the separate source-first audit
`.agents/state/353-elm-source-scope-audit.md`. Earlier false coverage acceptance
remains explicitly invalidated in `validation-results.md`.

## Decision and alternatives

Introduce a reviewed scope contract before executable construction. Retain the
existing manifest, generic runner, execution adapters and final readiness rule.
The additional stage separates source obligations from implementation choices;
it does not replace final independent review or deterministic checks.

Increasing repair counts or timeouts would allow more attempts without addressing
candidate-defined scope. Changing the model alone might improve generation but
does not repair the scope ownership boundary. Neither is the proposed correction.
Existing profile limits and the model baseline remain unchanged for comparison.

## Scope contract

1. A read-only scope author navigates the pinned repository using existing generic
   evidence tools. It declares environment obligations, cited exact source paths,
   their applicability and observable usability criteria. It does not generate
   installation scripts or select privileged runtime facilities.
2. The daemon assigns obligation IDs before review. A fresh independent reviewer
   starts from trusted whole-environment scope and
   repository evidence, without the executable candidate or author conversation.
   It independently follows development/test entry points and nested references,
   then assesses the obligations. Missing or contradictory evidence, unknown
   applicability and unproven required capabilities hold. Candidate-authored
   disclaimers and image convenience never authorize exclusions.
3. The daemon owns obligation IDs and binds the approved contract to source commit,
   inspected input hashes, complete path inventory digest and requested scope.
   Every admitted author/reviewer read is retained. All findings retain identity
   and must receive an explicit evidence-backed resolution. An assessment accepting
   coverage while retaining a blocking finding is invalid.
4. The reviewed contract records required usability criteria and runtime needs in
   generic terms. No core toolchain catalog, recognized filename list, project
   recipe or automatically selected service implementation is introduced.

An agent judgment is still required and fallible. This boundary makes its reviewed
scope explicit, testable and independent of a preferred implementation; it does not
claim deterministic discovery of every conceivable repository requirement.

## Construction and execution

The constructor receives the approved contract and actual execution capabilities.
It produces installation, activation, service lifecycle and clean checks against
those fixed obligations. Model-owned outputs cannot change obligation IDs,
requiredness, applicability or usability criteria. The daemon validates the full
mapping: every required criterion has at least one required executable check;
all references exist; required service actions and their evidence remain required.
An optional probe or version-only check cannot satisfy an operational criterion.
The final fresh reviewer evaluates that semantic mapping and performs a fresh
omissions review against the source, including evidence discovered during construction.

Use focused construction units keyed by obligation IDs rather than repeatedly
regenerating the entire contract. Each unit uses the existing bounded response
transport and validation. The daemon composes units and checks global ID uniqueness,
dependencies, activation and cross-unit service lifecycle. No unit executes project
commands before the final manifest receives independent coverage approval.

Construction may resolve an absent tool through installation or existing declared
platform facilities. If a required capability cannot be supplied, the result is an
explicit hold with the unsatisfied obligation and evidence, never an optional check.
No automatic host socket, device, privilege, network-policy or scope expansion is
permitted. This amendment does not add a VM runtime adapter or promise that the
current consumer image can run Elm's full service environment. The existing limited
software-VM feasibility experiment remains diagnostic evidence only.

The accepted executable manifest includes the scope-contract digest in its canonical
identity. Installation and clean verification use this same manifest digest, pinned
source, image/platform and capability identity. Installation may use the declared
network policy; the clean consumer retains fresh source/scratch, a read-only cache
and the existing verification policy. Every required check and applicable service
init/start/health/stop must run and pass. Agent approval cannot waive failed, unrun,
unknown or inconsistent execution evidence. Teardown failure prevents acceptance.

## Repair, provenance and compatibility

All stages share the original absolute attempt deadline, call/read/output limits
and two schema, two coverage and two execution repair caps. Focused units do not
receive fresh allowances. Restart restores consumption; it never starts a new
budget. Report the actual stage and stable reason for every hold.

Newly discovered obligations reopen scope review before execution and produce a
new contract and manifest digest. Existing required obligations cannot be removed
to pass. After any project execution, preserve the existing frozen required-command
rules, including unrun checks. A change that would alter frozen commands holds for
an explicitly authorized new attempt; it cannot disguise a scope revision as repair.

Version the scope contract and persisted stage state. Existing cache records lacking
the reviewed contract remain ineligible for new readiness acceptance and require
fresh review/verification; do not silently grandfather historical accepted evidence.
Source additions/deletions, changed inputs, scope, image/platform, capabilities or
contract content invalidate reuse. Preserve failed attempts and historical evidence.
Use secret references and existing redaction throughout; no source text or model
rationale is trusted as an instruction or emitted as an unsanitized diagnostic.

## Acceptance scenarios

1. A synthetic unfamiliar nested repository documents an operational service and
   several toolchains. A candidate declares client-only checks and says the image
   cannot run the service. Source-first review retains the operational obligation;
   construction holds or supplies a working implementation. No readiness is emitted.
2. The exact latest Elm defect is replayed: a required obligation points to an
   optional check. Admission rejects it with actionable bounded feedback and preserves
   its requiredness. No installer starts and no repair counter resets.
3. A complete contract covers the documented development/test areas identified by
   the pinned Elm audit, including service runtime, nested applications, validators
   and ordinary-command activation. Deployment-only/conditional activities require
   source-backed applicability decisions, never image-based exclusions. This fixture
   belongs in tests, not a runtime project catalog.
4. Scope review is accepted but construction omits a criterion, forges a reference
   or substitutes a version check for operational use. Structural mapping or final
   independent semantic review rejects it before execution.
5. A small synthetic project passes review, installation and every required clean
   check. Changed source inventory, contract, image or platform invalidates its saved
   result. A failed or unrun check, required service or teardown always holds.
6. Multiple construction units, malformed replies, review revisions and execution
   repairs consume one original budget. Cancellation/restart retains counters,
   deadline and owned resource cleanup, with no leaked credentials or runtime resources.
7. Retest pinned Elm first with the approved model/effort and 60-minute profile.
   Report source coverage, actual installation and every clean result separately.
   Only genuine whole-environment success permits the remaining pinned repository
   matrix. A capability hold is an accurate failure, not completion of the user goal.

## Delivery gate

This is an architectural amendment, not an approved implementation plan. After
written-spec approval, write and review the implementation plan, run repository
analysis, then implement with meaningful failing tests and full-diff adversarial
review. Do not merge PR #354 or mark the goal complete until the existing real
repository acceptance requirements are satisfied.
