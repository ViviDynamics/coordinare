# Unavailable source navigation and explicit correction budgets

User decision, 2026-10-19: fix the Dettmore source-read failure before rerunning
simulations, and allow eight schema corrections and four coverage revisions.
Execution repairs remain capped at two. This updates the earlier two-per-category
ceiling for explicit configuration; defaults remain two for each category.

The Dettmore run read `.opencode/skill`, a pinned Git symlink to an absent installed
skills directory, together with `.gitattributes`. The source provider correctly
returned unavailable content for the link; navigation incorrectly aborted the
whole discovery attempt. Model-selected navigation now returns a bounded indexed
`source_unavailable` error for that entry and retains valid text from the same
batch. Unavailable content receives no hash and cannot be cited as inspected
evidence. Missing required evidence still prevents coverage acceptance. Internal
mandatory reads remain strict; previously inspected content becoming unavailable
or any provider identity/access refusal still aborts. Resolution remains inside
the pinned repository, with no host filesystem fallback or ecosystem-specific rule.

The maximum configurable schema/coverage/execution allowances are now 8/4/2 in
configuration, discovery, CLI admission, runtime reservations and persisted journal
validation. Restart retains consumed counters and can only lower the original
maxima and expiry. Higher settings apply to fresh attempts, never revive a held
attempt or renew a deadline. Call, read, byte, probe, execution and teardown limits
remain enforced. Independent source-backed coverage review and deterministic
execution of every required check in a clean consumer remain mandatory.

Verification covers partial batches, strict mandatory reads, lost prior evidence,
provider refusal, pinned directory links with host-only targets, valid pinned file
links, category exhaustion, journal roundtrips and non-renewal on resume. After
exact-head preflight and CI pass, use fresh labeled Elm and Dettmore profiles with
explicit 8/4/2 allowances, preserving every prior result. Both keep the approved
3600-second total budget and 1800-second private probe batch ceiling. This patch
alone does not establish either repository's bootstrap acceptance.
