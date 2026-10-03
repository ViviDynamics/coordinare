# Define the coordinare to driver integration contract

Issue #515

## Scope
In: a normative contract document at
`specs/515-driver-integration-contract/contracts/coordinare-driver-contract.md`
covering (1) coordinare-side expectations of driver as a performer backend,
(2) the ask/clarification channel when the driver is a meta harness
(coordinare or qa-harness) rather than an end user, (3) compaction and
context-window visibility required of driver for the calling harness to be
deterministic. Plus a plan-document commit and a short pointer from
`docs/onboarding/04-harnesses-and-shims.md`.

Out: the driver backend adapter implementation (`SUPPORTED_BACKENDS` entry +
adapter class), any driver-side code change (compaction, cost, output field —
driver slice 2), and changes to the enforced dispatch-payload field registry.
Those are the next issue; this one defines the contract they must satisfy.

## Assumptions
- The deliverable is a document, not code; no executable test can fail on it.
  Verification is evidence-checking: every claim in the contract cites a real
  path/line in coordinare or driver, re-verified against the worktree.
- The contract is versioned with driver's machine contract (v1 today) and says
  so, so a mismatch fails at start (`--contract N`, exit 2) not at runtime.
- Ask resolution: the board round-trip stays the human channel; a meta
  harness answers structurally by resuming the session. The bot-author filter
  in `check_board.py` is a fact the contract works with, not around.
- Compaction: driver slice 1 has none; the contract makes observability a
  normative requirement on driver (event or result field) and defines what
  coordinare does meanwhile (`stop_reason=max_tokens` → `token_limit`).

## Tasks
- [x] 1. Research both sides (coordinare seams, driver CLI/wire contract): two
  parallel explore passes, evidence recorded in the contract's citations.
- [x] 2. Write the plan (this file) and commit it with the work.
- [x] 3. Write the contract document; tick each issue work item off inside it.
- [x] 4. Pointer edit in docs/onboarding/04-harnesses-and-shims.md.
- [x] 5. Preflight (doc diff → lint + any rows touching changed paths), push,
  PR with `Closes #515`.
