# Source review decisions with explicit grounds

Issue #353; PR #354. Status: written proposal for maintainer review.

## Goal and preserved policy

Complete pinned Elm bootstrap through independent source coverage review,
independent executable coverage review, installation and every required check in
a clean consumer. Keep the core independent of languages, frameworks, libraries
and repository names. Source acceptance establishes obligations; it does not
establish readiness. Every required executable check and service lifecycle
operation must still run and pass. No agent can waive a failed or unrun check.

This amendment changes the source review protocol only. It does not introduce
project recipes, change the constitution, narrow the requested environment,
increase budgets, edit repository sources, select another model, or renew an
old attempt. Dettmore is tested again after genuine Elm acceptance.

## Observed failure

The fresh `context4mib-reads2048-haiku55-elm60m-high64k` run on `8122b741`
held after 634.4 seconds, four source reviews and thirty model calls. It retained
nine open findings and never ran installation or a clean consumer. Cleanup and
the unchanged source pin were verified. The empty-citation schema mismatch that
exhausted its last repairs is corrected separately in `86e6f427`.

The third review described the finance criterion and its success condition as
present but kept its finding open until clean execution passed. Another decision
deferred resolution until scope acceptance itself. These create circular
dependencies. Other findings identified genuinely unread source and remain
legitimate blockers.

A separate one-call diagnostic rechecked 71 pinned input hashes and supplied the
same saved review evidence with a source-only reminder after that evidence.
It resolved the finance and fs-verity objections while retaining unread-source
objections. However, another decision still returned `status: open` with a
rationale claiming resolution. The diagnostic grants no acceptance and performs
no project execution. A reminder alone is not a reliable correction.

Current implementation locations are `environment_scope._messages`,
`environment_scope._validate_review`, `ScopeFindingDecisionProposal`,
`environment_response_schema._review_constraints`, and the generic finding ledger.
The existing validator checks verdict against statuses, but cannot determine
whether free-form rationale contradicts a status or describes a later phase.

## Alternatives and decision

1. More prompt reminders preserve the wire format, but the actual diagnostic
   still produced contradictory decisions.
2. Add another agent to reinterpret every review. This adds a new judgment loop
   and budget pressure without removing the ambiguous decision representation.
3. Require one structured source disposition per finding, with explicit grounds,
   then derive the generic ledger status deterministically. This is recommended.

The daemon must never infer acceptance from words such as "resolved", "runtime"
or "covered" in model prose. This proposal does not add a keyword classifier.

## Source-only decision contract

The source reviewer returns exactly one decision for each retained finding ID,
including previously resolved findings. Each decision has a finding ID, a single
disposition, nonempty unique inspected file citations, and bounded rationale.
There is no separately authored `status` field. Use a strict discriminated union:

| Disposition | Required evidence | Derived ledger status |
| --- | --- | --- |
| `covered` | One or more current criterion IDs, and an explanation tying the objection to their obligations and observable success conditions | `resolved` |
| `source_correction` | Cited source evidence explaining why the objection was mistaken; no modification of any established required criterion | `resolved` |
| `source_gap` | A concrete remaining source defect, its affected current criteria when known, and any explicit inspection requests | `open` |
| `duplicate` | Another retained finding ID, cited evidence and a rationale identifying the same objection | `resolved`, with the existing duplicate link |

A source gap must identify one of: missing obligation, unread source, source
conflict, unknown applicability, incomplete runtime needs, or incomplete observable
success condition. New findings must use the same source-gap representation.
Pending installation, pending execution and waiting for scope acceptance are not
source-gap kinds. A genuine source conflict about how an operation must run is
still a source gap; evidence of runtime success is not required to classify it.

All referenced criterion and finding IDs must belong to the current subject.
`covered` must reference at least one criterion; source correction must not
reclassify or remove an established required criterion. Duplicate decisions retain
the existing requirements: no self-link, no unknown target, and an independent
decision for the original. Citations must be independently fetched at the same
pinned commit, never borrowed from author cache or an earlier review.

Bounds remain the existing maxima: 32 findings/decisions, 128 current criteria,
eight requests per finding, existing UTF-8 text and aggregate context/contract
limits. JSON schema must expose representable cardinality and branch constraints;
local validation remains authoritative. Providers unable to carry the contract
must fail closed rather than omit required decision evidence.

## Admission, persistence and repair

Validate the complete review before publishing any ledger, investigation queue or
scope acceptance. Convert valid dispositions to existing generic decisions, then
apply the existing finding identity, duplicate, citation, verdict and ledger-size
checks. Unknown applicability and any open or new source gap prevent acceptance.
The reviewer still chooses its verdict; a contradictory verdict is rejected.

Persist the latest structured source assessment with its review sequence, scope
digest, source commit, inventory identity and complete reviewer input hashes.
The accepted source contract binds that assessment to the corresponding derived
ledger review. Recovery verifies the same normalization and bindings; it cannot
invent a typed assessment from historical rationale. Retain full prior ledger and
investigation history, original start/deadline, counters and cleanup ownership.
Keep this evidence within existing aggregate journal/contract bounds.

Use source protocol 4, journal version 9 and source contract schema version 2
for the new assessment. Read legacy
records for audit, but mark contracts lacking the new bound assessment as requiring
fresh source review under the new policy. Never label them newly accepted by
adding defaults. A pending or terminal legacy attempt cannot restart with renewed
budgets; existing terminal results remain terminal. A new attempt must be visibly
separate and retain the old evidence. This migration does not alter the executable
review protocol or convert old execution failures into passes.
Changing the source contract changes its digest and invalidates manifest/execution
evidence bound to the old contract. Obtain new bound executable coverage,
installation and clean-consumer evidence before readiness; never relabel old
execution evidence with the new digest.

Treat repository text, prior rationale and author responses as untrusted evidence.
Retain credential-reference validation and redaction. Persist source decisions in
private artifacts; expose only existing safe summaries to operators, not raw
rationale, source contents or credentials. No new shell execution or external
publication is introduced by reviewing or normalizing a decision.

Malformed decisions get exact, bounded field locations and fixed corrective
guidance within the original schema repair allowance. Preserve valid decisions,
the attempted scope and all prior findings while repairing. Do not repair by
inventing citations, dropping findings, converting an open decision to covered,
or increasing a quota. Exhaustion remains a terminal hold.

Structured output removes the redundant status/disposition disagreement, not all
possible model errors. A model can still misunderstand evidence or misclassify a
gap. Independent evidence, explicit grounds, bounded repairs and the later
deterministic execution gate remain necessary; no natural-language consistency
guarantee is claimed.

## Acceptance scenarios

1. A current required criterion covers an objection with adequate runtime needs
   and success conditions. A fresh reviewer chooses `covered`; normalization
   resolves the source finding without requiring runtime evidence. The environment
   remains unready until every later gate passes.
2. A finding identifies unread available source. The reviewer returns a concrete
   `source_gap` and inspection requests; acceptance holds until inspection and a
   subsequent independent review resolve it.
3. A mixed finding includes both missing source coverage and future runtime proof.
   The concrete source defect remains open. Retaining the runtime obligation does
   not excuse the source defect or make the runtime check optional.
4. An old-style `status: open` decision, a runtime-wait gap kind, an unsupported
   criterion ID, empty citations, or a resolved disposition with inspection
   requests is rejected atomically. Feedback identifies the invalid fields and
   preserves the attempted scope; no prose-based status rewriting occurs.
5. A duplicate remains linked to its independently adjudicated original. A source
   correction cannot remove a required criterion. Missing decisions and verdict
   contradictions hold under the existing finite budget.
6. Recovery rejects changed source/scope/input hashes or a typed assessment whose
   derived decisions differ from the saved ledger. Legacy acceptance is not silently
   upgraded; original spent counters and terminal outcomes survive migration.
7. Exact saved Elm evidence is replayed as a diagnostic, retaining genuine gaps.
   A new full pinned Elm attempt must then pass fresh source and executable reviews,
   preserve one manifest digest across installation and offline clean verification,
   execute every required check/service lifecycle successfully, and confirm cleanup.
   Diagnostic success or green CI alone does not complete this scenario.

## Delivery gates

Self-review this written amendment and obtain maintainer approval before writing
the implementation plan. The plan must identify RED/GREEN tests for each scenario,
schema transports, normalization, persistence/migration, and end-to-end readiness
rejection after failed or unrun checks. Obtain plan/execution-method approval before
implementation. Use existing preflight, independent code review and current-head CI
gates. Do not run another whole Elm attempt before the protocol correction and its
targeted evidence replay are verified. Original shipping acceptance remains intact.
