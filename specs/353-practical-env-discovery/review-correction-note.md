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
