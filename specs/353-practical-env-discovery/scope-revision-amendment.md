# Bounded revisions of source-defined scope

Issue #353 / PR #354. Status: written design approved by the user on 2026-10-19; implementation-plan review pending.
No implementation or additional model run is authorized by this document alone.

## Goal and observed failure

Complete the pinned Elm bootstrap through independent source/manifest coverage
review and successful deterministic execution of every required check in a clean
consumer. Keep Coordinare language, framework and library agnostic. Preserve the
approved model baseline, original deadlines, repair caps and isolation policy.

The `scope-protocol-elm60m-low` run on `a07c0fd9` stopped after 1,397.75 seconds,
12 successful model responses and 60 source reads. It did not run installation,
probes or clean verification. The original two schema repairs were exhausted;
2,202 seconds remained, so this was not an aggregate timeout.

Independent source review worked: it recorded `F0001`, identifying missing
prototype consumer/host/real-model end-to-end obligations. The author then had to
repeat 21 existing criteria to add the missing coverage. Its roughly 22 KB reply
duplicated the top-level `scope` key, using it both as an object and as a scope
description. The correction added an undeclared `scope_definition` field. Both
responses were correctly rejected. The source finding remains in the saved
ledger; there is no accepted scope contract or executable manifest.

Private evidence is retained under
`.agents/state/353-org-validation/scope-protocol-elm60m-low/elm/`, particularly
`chat-009.json`, `chat-011.json`, `chat-012.json` and `environment-attempt.json`.
The pinned source remains `bc224a78db6f669d56863599d653f14e7605de0b`.

This proposes a narrow amendment to [source-first coverage](coverage-first-amendment.md)
and [Task 4 of the approved plan](elm-bootstrap-fix-plan.md).

## Alternatives and recommendation

1. **Typed revisions, recommended:** retain the existing complete initial scope
   declaration. After a review objection, ask for bounded additions and explicit
   reassessments of unresolved applicability. The daemon retains existing criteria
   and constructs the next complete definition deterministically.
2. **Continue complete rewrites:** add more prompt examples. This preserves the
   current wire protocol but still makes a small correction repeat a large amount
   of immutable text. Recent runs show that formatting consumes the repair budget
   before the executable environment is even constructed.
3. **Change the model or structured transport:** potentially improve adherence,
   but this changes the approved comparison and does not remove redundant copying.
   It remains a separate experiment, not a substitute for explicit ownership and
   validation of revisions.

This amendment changes only source-author revisions. Executable construction
remains one complete response. Independent reviewers still inspect the complete
result, not just a change list. There is no author fan-out or new agent framework.

## Revision protocol

The first author response remains the existing `scope` envelope. A subsequent
author response must use a distinct strict envelope:

| Envelope field | Type and constraint |
| --- | --- |
| `scope_revision.base_digest` | Existing 64-character hexadecimal definition digest |
| `scope_revision.add` | Array of existing `ScopeCriterionProposal` objects |
| `scope_revision.reassess` | Array of objects containing exactly `criterion_id` and a complete `ScopeCriterionProposal` in `criterion` |
| `finding_responses` | Array of existing strict `FindingResponse` objects |

All envelope fields are required and extra fields are forbidden at every object
level. The arrays may be empty; a response can defend the existing definition
against a finding without inventing a criterion change. The existing open-finding
response requirements and independent review still apply. Use `scope_revision`
only for revisions; reject a complete `scope` declaration in this phase. The
implementation plan must spell out the corresponding native JSON schema and
canonical-JSON transport tests before implementation.

- Require an exact base digest from the retained definition. Missing, stale or
  invented identities hold. Do not silently rebase a model revision.
- Start with all retained criteria. There is no delete operation and no operation
  that edits a required criterion. Existing required definitions and IDs remain
  exact. The existing `preserve_scope` check remains an independent guard.
- `add` contains complete criterion proposals with no model-assigned IDs. The
  daemon computes their IDs. Duplicate definitions or collisions are rejected.
- `reassess` may target only an existing `unknown` or `not_applicable` criterion,
  once per revision. It cannot target a required criterion. Replacement content
  needs inspected source evidence and a complete applicability rationale; its ID
  is computed by the daemon using the unchanged criterion-identity rules.
- Finding responses retain the existing strict shape and daemon finding IDs.
  Their `affected_ids` may name retained current criteria. As already permitted,
  the list may be empty when newly added/reassessed criteria have not yet received
  daemon IDs. Do not fabricate IDs or silently translate a stale response target.
  The reviewer receives the complete canonical result with all assigned IDs.
- Validate every citation against actually inspected file content. Listings are
  navigation evidence. Keep all prior input hashes, new reads and inventory/source
  identities; new evidence cannot erase earlier evidence.
- Limit a revision to 16 total additions/reassessments, matching the existing
  bounded-patch convention. Preserve the existing 128-criterion, 256 KiB definition
  and output/context bounds. Reject oversized revisions with bounded diagnostics;
  do not split them automatically or acquire another allowance.
- Reject duplicate JSON keys, extra fields, malformed proposals and invalid
  references. Do not salvage a response by stripping metadata or selecting one
  duplicate value. Return trusted codes and precise field locations without raw
  source text or candidate values.

Apply a valid revision atomically, producing a new complete `ScopeDefinition`.
The canonical final scope, finding review, manifest mapping and execution report
formats remain unchanged. A failed revision cannot partially change the base.

## Review, budgets and recovery

After each valid revision, start a fresh independent whole-source coverage review
with the complete definition, retained finding ledger/responses and pinned evidence.
The reviewer must adjudicate every open finding and inspect omissions beyond the
author's changes. Unknown applicability, unresolved findings or incomplete evidence
still prevent acceptance. No agent can waive failed or unrun executable checks.

Every tool read, author call, schema correction and coverage revision consumes the
original attempt budget. Do not increase any cap, extend the absolute deadline,
reset a counter or implicitly start a new attempt. No project command runs before
the existing final independent executable-manifest review accepts coverage.

Persist the wire-protocol version, exact base identity, canonical definition,
responses, ledger and consumed counters before another model call. Advance the
journal schema for this state. An in-progress older source-author journal must
hold for an explicit reconciled restart with its original evidence and consumption
archived; do not reinterpret its pending full response as a revision. Previously
accepted canonical scope contracts do not change merely because the internal
author protocol changes; all existing reuse and readiness checks still apply.

Held diagnostics must report open source-scope findings as well as executable
manifest findings. The saved `F0001` must be visible even when executable discovery
has not begun. Report the actual stopping stage, used counters and remaining time.

## Acceptance and scope limits

1. Minimize the saved failure into a regression: a valid base retains its required
   criteria exactly while a small revision adds the omitted obligation and responds
   to the existing finding. No full declaration copy is necessary.
2. Reject stale digests, unknown/duplicate targets, attempts to reassess required
   criteria, duplicate IDs, unread citations, invalid response references, duplicate
   JSON keys, extra metadata and revisions exceeding 16 changes. The base stays intact.
3. Resolve an unknown criterion only through explicit source-backed reassessment
   and fresh independent review. A declaration based on image limitations or a
   directory listing cannot make a required capability optional or out of scope.
4. Crash/restart tests preserve the exact base, source evidence, finding identity,
   original expiry and consumed work. Schema and coverage exhaustion still hold.
5. Native-function and canonical-JSON transports expose the same phase-specific
   schema, limits and diagnostics. Replay saved malformed replies offline as held
   evidence; no replay manufactures production approval.
6. After focused regressions and required CI gates pass, run a separately labeled
   fresh test against pinned Elm using GLM-5.3-Flash low effort and the approved
   3,600-second aggregate, original repair caps and proven private runtime policy.
   Record actual independent review and installer/clean-consumer results. Success
   means every required executable check ran and passed with confirmed teardown;
   a small synthetic revision test or accepted scope alone is insufficient.

Do not add project recipes, language detection, broader repository runs, model
changes, larger budgets, privileged runtime access or unrelated refactors. The
constitution is not amended implicitly. Elm remains the first acceptance case;
PR #354 remains unmerged until genuine bootstrap success and shipping gates pass.
