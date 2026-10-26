# Reviewer-scoped source conflict correction

Maintainer decision, 2026-10-26: an independent source reviewer can reopen exactly the
criteria it names in a current typed `source_conflict` finding or `source_gap` decision.
This corrects draft requirements inferred from contradictory or superseded source guidance.
It does not require changing the pinned repository or choosing a model-specific workaround.

## Authority and preservation

The assessment must reproduce the latest retained ledger review and match the current
source commit, inventory digest, definition digest, and verified source hashes. Only open
conflicts in that assessment authorize reassessment. Historical findings, resolved or
duplicate decisions, other gap kinds, author responses, and executable findings do not.
The reviewer must have inspected the target criterion's cited pinned source files.

The author may propose a corrected required criterion or a source-backed not-applicable
criterion for those exact IDs. Every other required criterion remains unchanged. The
previous definition and findings stay in immutable review history. Sharded authors receive
their authorized IDs and complete target criteria; cross-owner revisions use the existing
global revision authority. An author cannot change another group's required criteria.

## Independent acceptance

A reassessment does not close its finding or accept the scope. Changed criteria and newly
retained evidence receive fresh independent source review before contract admission.
Unresolved findings and unknown applicability still prevent acceptance. A newer review
supersedes the earlier reassessment authority; it never grants permanent release.

The remote-state and runtime-network release rules remain unchanged. Source conflict
correction does not waive executable coverage, checks, services, installation, offline
consumption, authorization, or cleanup. Calls, reads, revisions, deadlines, context, and
memory limits remain unchanged. Public tests use synthetic repositories and commands.

## Acceptance cases

- A current new or retained source-conflict finding permits only its named reassessment.
- Both corrected required and not-applicable proposals retain unrelated required criteria.
- Superseded, resolved, duplicate, stale-digest, source-drift, author-only, and nonconflict
  attempts fail closed or confer no reassessment authority.
- Both single and sharded revisions preserve the open finding until fresh independent review.
- Restart preserves the typed review and its authority without changing counters or deadlines.
