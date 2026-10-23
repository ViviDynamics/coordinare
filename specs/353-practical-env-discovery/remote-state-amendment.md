# Remote-state obligations amendment

Status: decided by the maintainer on 2026-10-22, after Dettmore v13.

## Problem

Dettmore v13 deadlocked on a required criterion: branch protection and the CODEOWNERS errors API must be verifiable
through an authorized GitHub CLI session.

Clean verification runs offline and without credentials, so no check can ever satisfy that criterion:

- A required, unscoped check always fails offline.
- Making the check scoped or optional invalidates the criterion's mapping.
- Dropping the check removes a required obligation.

## Decision

An obligation that only an external service's state can satisfy is outside the development and test environment.
Examples are settings or data held by a code host (branch protection, required reviews, its CODEOWNERS or rules
APIs), a live third-party API account, and cloud account state.

Such an obligation is classified `not_applicable`, with a rationale that names the service.

Everything the repository itself installs, builds, starts or caches stays required. That covers tools, services,
container images and advisory databases, even when installation fetches them from the network.

## Mechanism

Authors classify these obligations directly, using the shared rule in the authoring guidance.

Required criteria remain protected. A revision may not edit or remove a required criterion, with one exception:

1. An independent reviewer raises a finding with category and `gap_kind` `remote_state`. Its `affected_ids` name
   the criteria concerned.
2. Only those criteria, which the reviewer has released, may be reassessed, and only out of the required set.

Every preservation check honours the released set:

- revision application;
- `preserve_scope`;
- the journal checkpoint;
- manifest obligation preservation.

The released set is derived from the scope finding ledger, so each release is traceable to the review that justified
it.
