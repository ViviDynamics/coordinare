# Preserve human clarifications and resolved merges

Issues #563 and #564

## Scope
Deliver retained issue comments to every clarification renderer, preserve resolved merge commits during safe pushes, and bound executor error logging so failures publish promptly.

## Assumptions
- Legacy question/answer entries retain their existing content.
- A fetched remote head that is already an ancestor needs no rebase; ordinary push still rejects concurrent updates.
- Errors include a redacted terminal summary, with no traceback locals in routine executor logs.

## Tasks
- [x] Prove body-shaped clarification omission at all actual prompt/CARD.md boundaries, then share rendering with source identity.
- [x] Prove resolved merge publication fails in a real Git repository, then skip unnecessary rebases while retaining divergence protection.
- [x] Prove executor error logging is unbounded and verify a bounded event plus terminal failure.
- [ ] Run full suites, preflight, adversarial review, CI, merge, and deployment verification.
