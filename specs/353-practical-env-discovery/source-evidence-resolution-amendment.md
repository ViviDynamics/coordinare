# Source evidence resolution and actionable revision feedback

Issue #353; PR #354. Status: approved as written by the maintainer on 2026-10-20.

## Goal and preserved policy

Complete pinned Elm bootstrap through independent source and executable coverage
review, installation and every required clean-consumer check, then the remaining
shipping gates. This amendment changes only how pinned source absence and author
correction feedback are expressed. It keeps the source review decision protocol
(source protocol 4, journal 9, source contract 2), every budget, every required
obligation and every execution gate. It adds no project recipe, language or framework
knowledge, repository name or model-specific behavior. No agent can waive a gap or a
failed or unrun check.

## Observed failure

The `source-decisions-cov8-direct-haiku55-elm60m-high64k` attempt on `79d22f86`
(real endpoint, eight coverage revisions) held after 1,047 seconds at the schema
repair limit, before any executable review. Seven independent source reviews were
admitted without contradiction and one finding was resolved as covered.

1. The other finding stayed a `source_gap` of kind `unread_source` for six reviews.
   All eight delegated inspection requests named two paths, and both were absent
   from the pinned inventory. One path was requested seven times. Each time the
   daemon recorded only `absent`. Both names exist elsewhere in the inventory under
   a subdirectory prefix, so the reviewer had resolved a nested project's relative
   reference against the repository root. Nothing in the absence evidence helped it
   find the real file, and nothing prevented it from requesting the same absent path
   again, so the gap could never close.
2. The author spent its eight schema repairs on correctable output: two identical
   proposals with a malformed source path in one criterion (feedback was a generic
   `invalid_contract_value`), two proposals citing uninspected files (specific
   feedback that worked), one response-ID correction, and three revisions that
   resubmitted the whole scope as 38, 35 and 35 additions against the existing limit
   of 16 combined add/reassess operations. The `too_long` feedback did not state the
   limit or that established criteria are retained automatically.

## Decision

### Absence is answered evidence that points to candidates

When a read request names a safe path that is absent from the complete pinned
inventory, the absence result also lists inventory files whose final path component
equals the requested one, at most eight, with a count of further matches. Ordering
(executor's call): most trailing path components shared with the request first, then
shorter paths, then lexical order. A nested copy of the whole requested path therefore
ranks first even when the file name is common.
Matching is purely by path text: no language, file type or project convention is
interpreted. The candidates are names from the pinned inventory, not file contents,
and are delivered with the existing investigation evidence to the author and to the
next independent reviewer. They are hints, never citations: a file still has to be
inspected before it can be cited.

A path proven absent at a pinned source commit and inventory digest stays absent
for the attempt. A later inspection request for a path already proven absent in
the same attempt is rejected atomically, with fixed feedback listing that path's
recorded candidates. The reviewer can then request a candidate, read it with
source tools, or choose another disposition. A `source_gap` of kind `unread_source`
must carry, in the same decision, at least one inspection request for a path present
in the pinned inventory (a file, or a directory page). A request for a path not in
the inventory is still allowed the first time, because its absence is useful
evidence. Every other gap kind is unchanged. A rejected review contributes nothing:
its requests are neither recorded nor counted (executor's call). Candidates are
already visible to the first review after an absence, so a repeat rejection can
only follow evidence that named alternatives; exhausting the schema repair
allowance remains a terminal hold, as today.

These rules use the existing finite schema repair allowance. They do not resolve
any finding automatically, convert a gap into covered, or treat absence as proof
that an obligation does not exist.

### Correction feedback states the rule that was broken

Feedback stays bounded and never echoes candidate values.

- A revision exceeding the combined operation limit is told the limit (16), how
  many operations it submitted, that established criteria are retained without
  resubmission, and that additions are only for new obligations, with the remainder
  to follow in a later revision. The same applies to reassessments. As today, the
  over-limit revision is rejected whole with nothing applied, only add and reassess
  operations count toward the limit, and the retry consumes a schema repair, not a
  coverage revision. Only the feedback changes.
- An invalid source path in a criterion, finding, decision or response is told that
  each source must be one exact, repository-relative, inspected file path: no
  directories, `./` prefixes, trailing slashes, URLs or annotations.
- Existing specific feedback (uninspected citations, response IDs, reassessment
  targets, dispositions) is unchanged.

## Persistence and recovery

Absence results gain an optional bounded candidate list and further-match count;
journal version 9 and source contract 2 are unchanged (executor's call). A result
recorded before this change has no list and is kept as is: it is less helpful but
not wrong, and nothing is upgraded or invented for it. A recorded list is derived
again from the pinned inventory on recovery and must match exactly, so a tampered or
stale list is refused. The investigation queue's history, bounds and spent counters
are preserved.

## Acceptance scenarios

1. A reviewer requests a nested project's relative path from the repository root.
   The result is `absent` with the real nested path as a candidate. The next review
   can request or read the real file. No citation is accepted for the candidate
   until it is inspected.
2. A reviewer requests a path already proven absent in this attempt. The review is
   rejected atomically with feedback naming the recorded candidates. The ledger,
   queue and counters are unchanged apart from the existing schema repair.
3. An `unread_source` gap whose only requests name absent paths is rejected. One
   with a request for an existing inventory file or directory is admitted.
4. A revision with 38 additions receives the operation limit, the submitted count
   and the retention rule, and preserves its valid content when the author retries.
5. A criterion citing `./README.md/` receives the exact-path guidance.
6. Recovery rejects a changed candidate list or a candidate absent from the pinned
   inventory, and keeps the original counters and deadline.
7. A fresh labelled pinned Elm attempt then runs with unchanged controls. Readiness
   still requires accepted source and executable coverage, one manifest digest
   through installation and an offline clean consumer, every required check and
   service lifecycle step passing, and confirmed cleanup.

## Delivery gates

Maintainer approval of this written amendment, then an implementation plan with
RED/GREEN tests for each scenario and its approval, then native implementation,
independent review, exact-head preflight, quality guard and current-head CI before
any further live attempt.
