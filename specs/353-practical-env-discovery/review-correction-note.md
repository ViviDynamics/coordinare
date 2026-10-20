# Reviewer correction and simulation transport

User decision, 2026-10-20: implement the proposed bounded fixes before another
full bootstrap simulation. Preserve independent review, every required executable
check, the original attempt deadline, repair limits and all previous evidence.

Haiku Elm held after 591 seconds because reviewer corrections repeatedly supplied
`duplicate_of` with an `open` decision, and sometimes added more findings than the
remaining ledger capacity. The wire schema allowed these combinations and the
scope correction returned a broad error without explaining the rejected field.

Scope and manifest review schemas now describe two valid decision forms: open
with no duplicate target, or resolved with an optional retained target. Decision
IDs and counts remain bound to retained objections. The new-finding allowance is
32 minus the retained count. Local validation still rejects self/unknown targets,
cycles, unread evidence, invalid history, oversized ledgers and false acceptance.
Exact indexed status/target diagnostics explain corrections without exposing raw
source content. Review guidance directs existing objections to their retained
IDs, rather than re-adding paraphrases. No automatic semantic deduplication occurs.
Resolving a duplicate never resolves its original: an open original still blocks.

Haiku Dettmore held after three HTTP 524 responses at the public Cloudflare
endpoint, well before its 3600-second attempt deadline. The existing authenticated
local connection is a kubectl port-forward to `svc/litellm`; its model catalog
matches the public endpoint, including `claude-haiku-5-5`. Replaying the exact saved
failed request locally returned in 48.371 seconds with a valid original response
schema. This diagnostic ran no project commands and grants no readiness evidence.
Fresh simulations will use this existing local connection, the same Haiku alias,
high effort, source pins, runtime base, strict JSON schema, 32768 output ceiling,
3600-second attempt/request/phase budgets, 1800-second private probe batch and
8/4/2 schema/coverage/execution repair limits. Credentials remain in process memory.
No infrastructure configuration or deployment changes are needed for this test.

Verification includes red/green regressions for field feedback, strict wire
constraints, remaining finding capacity, immutable ledger rejection and a valid
duplicate correction that retains the blocking original. Replay the saved Elm
review state independently against the corrected contract; a valid revise verdict
is protocol evidence, not scope acceptance. Complete exact-head preflight and CI
before fresh full simulations. All original clean-consumer readiness gates remain.

The saved Elm review-state diagnostic completed in 91.766 seconds using two model
calls and one correction for invalid source-tool arguments. Its final assessment
was structurally valid and returned `revise`, preserving original blocking
objections while consolidating duplicate entries. It did not establish scope or
bootstrap acceptance. The Dettmore replay returned a source-read tool envelope,
not a final manifest; the point of that diagnostic was validating the unchanged
request transport and response schema. Focused regressions: 153 passed. Strict
mypy: all 344 source modules passed. Independent delta review found no Critical
or Important issue; its suggested manifest-capacity test is included. Full-PR
review and genuine clean-consumer project acceptance remain outstanding.

The fresh local-ingress simulations exposed two different finite bounds. Elm's
fifth HTTP-200 response exhausted its 32768 output tokens and truncated the initial
scope JSON (170.72 seconds elapsed). A single 65536-token saved-request diagnostic
returned a valid source-read request, which does not prove a final scope can be
authored. No scope, manifest, installation or clean verification was accepted.

Dettmore completed 18 model responses and three independent scope reviews before
holding during revision evidence preparation (566.52 seconds elapsed). An exact
saved-response replay, with no model or project execution, reproduced the cause:
the same author inspection accumulated 264 reads across authoring and revisions,
exceeding its 256-per-phase quota even though the attempt used only 470 of 512
reads. Its byte count was below the 4 MiB limit. Each revision now creates a new
inspection, just as each independent review does, and fetches the complete required
evidence again. It retains the original shared attempt budget and source-hash
ledger; it resets no attempt counter, deadline, repair allowance or finding.
Missing or changed retained evidence still holds. This correction has no stack
specific behavior and raises no configured limit.

With that correction, all 18 saved Dettmore response messages and schemas replay
unchanged. The previously failing evidence preparation completes with all 71
retained inputs and 485 total reads, reaching the next unsaved model request.
This is evidence of removing the erroneous phase-bound failure, not coverage or
bootstrap acceptance. Regression coverage verifies a successful revision across
separate phase quotas and refusal when the unchanged aggregate read budget runs
out. Both failed full runs remain retained, with owned image/registry/source
cleanup confirmed. Any next full run uses a fresh labeled profile.

On 2026-10-20 the user approved separating source coverage from runtime-proof
checks, investigating outstanding source references before resubmission, and
batching stale criterion-reference diagnostics while preserving attempted changes.
This corrects the documented source-first phase boundary; the constitution and
final readiness policy are unchanged. Shared navigation guidance now stays source
only. Executable construction and final review retain capability-probe and
operational-evidence instructions. Source messages explicitly carry their limited
phase authority and retain required capabilities with observable success conditions.
Unknown applicability, unread relevant sources, contradictory requirements and the
source portions of mixed findings still block. Each finding needs an explicit
independent decision; no category is automatically closed or exempted.

Revision validation now reports every invalid response reference in one bounded
correction, with indexed paths, validated finding IDs and invalid counts. It prints
current computed IDs once and omits arbitrary invalid values. Definitions and
responses remain uncommitted on rejection. Correction guidance preserves attempted
additions/reassessments while fixing responses; deliberately defending an existing
definition with empty operations remains legal and independently reviewable. There
is no automatic ID remapping, partial commit, repair reset or new correction protocol.

A saved first-revision replay against actual Haiku 5.5 completed in 31.481 seconds:
one model correction preserved all three attempted reassessments and passed strict
revision validation. It was a separate diagnostic, not a resumed bootstrap or scope
acceptance. Replaying all 22 original responses offline retains exactly the same
scope definition, ledger, 308 reads and final coverage-limit hold. New regressions
cover phase authority, required unprobed operations, all stale locations, unchanged
state during rejection and retention of corrected reassessments. Focused suites:
207 passed. Strict typing: 344 modules passed. Independent scoped review: no
Critical, Important or Minor findings; 148 tests passed. Full-PR review and actual
Elm installation/clean-consumer success remain outstanding.

The fresh 2 MiB Elm run reached independent scope review in 309.54 seconds, then
held with `scope_review_indeterminate`: eight source-backed objections, no extra
reviewer source reads and none of the four coverage revisions consumed. Its
mandatory evidence was a starting set of 51 inputs. The reviewer identified
specific unread references and conflicts but described needing more information
as a terminal inability to judge. No probe, installer or clean consumer ran;
owned image/registry/source cleanup was confirmed.

The approved source-investigation correction now also explicitly guides the
reviewer's continuation and verdict choices: inspect available source when more
information is needed; request revision for actionable source-backed omissions
or corrections; retain indeterminate for genuine inability to judge or continue
safely. The context carries those distinctions. Deterministic routing is unchanged:
an actual indeterminate response always terminates the attempt, even with unused
revision allowance, retains findings and creates no accepted scope. No model
judgment is automatically converted or finding automatically resolved.

Against the saved initial-review input, actual Haiku 5.5 using the corrected
product prompt returned a structurally valid `revise` assessment with six new
blocking findings in 13.927 seconds (one call, no schema repairs or additional
source reads). This separate diagnostic used the initial review's empty ledger;
it does not amend or resolve the eight findings in the held original attempt.
It establishes protocol continuation, not coverage or readiness. The caller still
requires author investigation and a fresh independent review. Regression suites:
209 passed; strict typing: 344 modules. Scoped independent audit: no findings,
87 tests passed. Genuine pinned Elm bootstrap remains outstanding.
