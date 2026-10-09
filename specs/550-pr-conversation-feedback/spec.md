# PR conversation feedback

Issue #550. The user authorized shipping all interaction-test follow-ups and then rerunning the live scenarios.

## User stories and acceptance

### US1 — Human request (P1)
A configured human reviewer posts an ordinary conversation comment requesting a regression or fix. Coordinare classifies the request and sends it to the existing feedback performer with comment ID, URL, author, and `source=pr_comment`. Ordinary comments never grant merge approval. Configured trusted bots may also request work. Untrusted authors and automated status posts are ignored under the existing reviewer authorization policy.

### US2 — Polling and edits (P1)
Unchanged comments trigger one turn across repeated polling and daemon restart. A changed request is classified again; unchanged-body edits do not repeat work. Acknowledgements, approval-like comments, and automated status posts do not dispatch. Multiple comments by the same author remain separate requests and are not superseded as submitted reviews are.

### US3 — Recovery (P1)
A request accepted before restart is retained through the existing durable feedback batch. Failures and incomplete polling do not advance beyond unread comments. Polling and classification use bounded per-cycle work.

## Functional requirements

- FR1: Poll the PR conversation through the PR number, separately from linked issue comments and submitted reviews.
- FR2: Apply configured human/trusted-bot authorization before classification; exclude untrusted authors and automated/self status posts.
- FR3: Classify request, acknowledgement/approval, and noise; use deterministic fallback when inference is unavailable.
- FR4: Preserve provenance in the performer feedback payload and in operator activity.
- FR5: Ordinary comments cannot authorize merge; submitted human approvals retain existing semantics.
- FR6: Store edit-aware processed comment versions durably per PR/card; legacy snapshots initialize empty.
- FR7: Replay accepted feedback through replacements without repeating completed requests; preserve unrelated submitted reviews.
- FR8: Reevaluate changed bodies, preserving requests independently of same-author review supersession.
- FR9: Limit classification to five new comments per tick and total polling/classification to 20 seconds; timeout leaves unaccepted requests eligible.
- FR10: Document supported comment behavior and demonstrate request/noise/edit/restart cases in automated tests and the final live retest.

## Scope and assumptions

Requests are supported rather than merely warning about an unsupported channel. This matches the user's intended human interaction test. Questions that do not ask for work follow existing clarification handling where applicable. Board pause, external close, and submitted-review retention are owned by #547–#549/#551/#552. No dependencies or new approval policy.

## Success criteria

SC1: One dispatched request for an unchanged comment across three polls and snapshot restore.
SC2: One additional request for a changed body, with source identity preserved.
SC3: Zero dispatches for acknowledgement/noise/untrusted author; zero merge decisions caused by conversation approval text.
SC4: No lost request in failed fetch, bounded partial processing, or restart after acceptance.
SC5: Mocked long-running fetch/classification stops within the configured 20-second budget; tests use controlled clocks, not long sleeps.
