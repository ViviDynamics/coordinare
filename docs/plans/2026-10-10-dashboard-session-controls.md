# Dashboard human controls reach the owning session

Issues #48 and #49

## Scope

Fix existing restart, skip and veto endpoints in multi-card and multi-symphony mode. Preserve legacy single-card requests, role resolution and authorization. Refuse ambiguous or invalid targets. Do not change lifecycle policy, review or CI gates, configured roles, or sample code.

## Assumptions

An unqualified request can target the sole eligible live session. With multiple eligible sessions the caller supplies `card_id`; otherwise refuse without mutation. A board-paused, stale or missing card is ineligible. Current symphony working state and other symphony runtime state retain their own cards. The configured graph lifecycle is shared, while a restored continuation belongs to the card. A valid explicit restart discards obsolete continuation; ordinary restoration retains it.

## Tasks

- [x] Reproduce flat-state rejection and misplaced acknowledgements with real endpoint requests.
- [x] Add failing endpoint tests and route accepted commands to the owning session.
- [x] Preserve a newer accepted command across own and sibling fanout and single-graph admission/fallback writeback; consume each request once.
- [x] Reject retired aggregate cards while restored-session ownership is awaiting board routing.
- [x] Record the exact command consumed inside the compiled graph, preventing replay after recovery while retaining a newer distinct request.
- [x] Verify persisted receipts, unchanged public responses, paused/ambiguous refusal and cross-symphony isolation.
- [x] Retain late flat-state commands with distinct receipts; never replay consumed commands or transfer commands to a replacement card.
- [x] Reproduce three explicit restart boundaries that skip configured stages; clear obsolete continuation only after valid restart.
- [ ] Run configured checks, full suite, adversarial review, fresh Copilot and current-head CI before merge.
- [ ] Verify actual publication/deployment and replay the original authenticated restart interaction and nearby regressions.
