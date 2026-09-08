# Phase 0 Research: card-less performer roles

Every decision below was taken against code read at `53c67b3`. Where a
precedent exists in the tree it is named, because copying a merged pattern is
cheaper and safer than inventing one, and because each of these precedents
carries a comment recording a bug it already fixed.

## D1. How a run is started

**Decision**: the daemon builds a `card_context` dict by hand, synthesises a
`WorkspaceInfo(path=None, branch, repo_url, github_token)`, and calls
`svc.dispatch_card(card_context, workspace_info=...)` directly, bypassing the
`dispatch_performer` graph node.

**Rationale**: the graph node cannot do it. Its body guards on
`if not isinstance(card, dict) or github is None or not performer_stage:` and
sets `phase="idle"`, so a card-less dispatch through the node is a no-op rather
than an error. `_execute_wiki_init_dispatch` in `daemon.py` already does exactly
the hand-built variant for a card-less documenting run, including the
`path=None` self-clone whose comment explains that a card-less dispatch never
flows through `WorkspaceManager.prepare()` and so must carry its own token.

**Alternatives considered**: routing through the node with a synthetic card dict
would satisfy the guard, since it tests `isinstance(card, dict)` rather than
truthiness, but it would then acquire a per-card mutex, a slot, an
`active_sessions` entry and a stage, all of which are lies about a run that owns
no card, and it would drag in the pre-dispatch rebase and multi-pull-request
divergence checks. Rejected. Copying `env_bootstrap` instead of wiki-init was
also rejected: it carries a bespoke typed payload plus a transport-level
discriminator branch, neither of which these roles need.

## D2. Which performer service runs it

**Decision**: reuse an existing performer service resolved from the stage-keyed
map with a documented probe order, and take the backend and model from the
role's own configuration when it is set. Register no new performer pool.

**Rationale**: wiki-init reuses `performer_services["documenting"]` and the
tech-writer persona rather than registering anything, which keeps the change off
the performer-pool composition path entirely. The bootstrap dispatch shows the
matching precedent for resolving backend and model by probing a role list.

**Alternatives considered**: a dedicated performer pool per role would mean
touching pool composition and giving operators two more things to configure
before either role works at all. Rejected as cost with no benefit for a
read-mostly run.

## D3. How the workflow is selected

**Decision**: the hand-built `card_context` sets `"workflow": "advocate"` or
`"curator"` directly, and both names join `SUPPORTED_WORKFLOWS` in the performer
and `KNOWN_WORKFLOWS` in coordinare. Unlike roles 164 through 172 the workflow is
not optional and there is no prose fallback.

**Rationale**: `Score.workflow` is a plain field, and `handle_dispatch` builds a
`WorkflowAdapter` whenever it is non-empty, so a card-less dispatch needs no new
selection machinery. The default-off flag that every previous role workflow
carried existed to protect a shipped prose path; the curator has none, and the
advocate's prose path is being deleted rather than preserved, so a flag would
guard nothing. Both names still join the registries so a typo in configuration
fails at load, and so `is_supported_workflow` stays the single source of truth.

**Alternatives considered**: a `workflow:` config flip per role, for symmetry
with the other eight. Rejected: a flag whose off position selects a deleted code
path is a trap, not a safety net.

## D4. The terminal status, and the trap it avoids

**Decision**: add `advocate_complete` and `curation_complete` to the closed
`PerformerStatusType` literal, exclude both from `FAILURE_STATUSES`, and give
each role its own branch in `handle_status` placed **before** the shared tail.

**Rationale**: this is the sharpest edge in the feature. `TERMINAL_STATUSES` is
derived from that literal, so a status missing from it never terminates the
job's poll loop and the run hangs until coordinare reaps it. Worse, the role
cascade in `handle_status` ends in the implementer path that runs lint, pushes
the branch and opens a pull request; the comment above that code states that
every other role returns earlier, which is precisely the invariant a new role
breaks by default. A role added without its own branch would silently open pull
requests from a run that is supposed to be read-mostly.

**Alternatives considered**: reusing an existing terminal status such as
`docs_committed`. Rejected: it would make the two roles indistinguishable in
logs, metrics and the completion handler, and it claims a commit that never
happened.

## D5. Where the outcome is recorded

**Decision**: extend `EnvCacheState` and `EnvCacheStateSnapshot` with a per-role
block, keep the in-flight marker on the in-memory model only, and bump
`CURRENT_SCHEMA_VERSION` from 19 to 20. Snapshots from v1 to v19 load with
defaults and need no migration step.

**Rationale**: that model is already the symphony-scoped bag for out-of-lifecycle
state; the wiki-init gate put six fields there for this exact reason and bumped
the schema to 13 doing it. A new top-level model would need its own load path,
its own merge behaviour across the multi-session fanout, and its own tests, to
hold the same shape of data.

**The bump is not optional.** The persisted subset gains fields, and the loader
knows the lowest readable version, so shipping new persisted fields without the
bump would leave a snapshot whose version claims a schema it does not have.

**Alternatives considered**: keeping everything in memory, as `advocate_history`
does today. Rejected outright: `advocate_history` is absent from every snapshot
model, so a restart re-scans every open issue, and only the GitHub label
prevents a duplicate reply. That accident is survivable for a role that labels
what it touches, and unacceptable for one that mutates a board.

## D6. The in-flight marker's ordering

**Decision**: set the marker before calling the dispatch, roll it back inside an
`except` around the call, and never persist it.

**Rationale**: both existing card-less paths document this. The bootstrap code
carries a comment explaining that setting the flag after the dispatch clobbers
the reset a synchronous failure performs, producing a stale-in-flight deadlock
that wedges the role forever; wiki-init's `check_and_trigger` shows the same
shape in three lines. The snapshot deliberately omits the equivalent bootstrap
flag so a crash mid-run cannot leave a permanently stuck marker on disk.

## D7. Forcing the snapshot flush

**Decision**: each completion handler calls the state's `snapshot_save_fn` after
writing its outcome.

**Rationale**: the daemon's save is gated on a lifecycle signature changing, and
a card-less completion changes none, so an outcome recorded in that window is
lost to a restart. `on_bootstrap_complete` already ends with an explicit
`snapshot_save_fn` call and a comment naming this reason. Not copying it would
re-introduce a bug this repository has already paid for once.

## D8. Where the documentation comes from

**Decision**: the advocate reads its configured documentation paths from the
working copy on disk and records the path and content of each file it read.
Missing files are tolerated and recorded as unread, matching today's behaviour.

**Rationale**: `handle_dispatch` calls `clone_repository(score)` unconditionally,
before any role branching, so every run has a real working tree with the default
branch checked out and history unshallowed. The current per-file GraphQL fetch
asks the API for files that are already on disk. Reading from disk is cheaper, it
costs no rate limit, and it makes the grounding gate honest, because the set of
documents the answer may cite is a set the run physically holds.

**Caveat carried into the plan**: the documentation branch is configurable and
defaults to the default branch. The clone guarantees the default branch and the
run's own branch, so a non-default documentation branch needs an explicit fetch.
The plan treats a configured non-default branch as a fetch step, not as an
assumption that it is present.

## D9. The advocate's gate

**Decision**: a reply survives only when it names at least one document and
every document it names is one the run read. A judgement about an issue the run
did not send is discarded. Anything that fails escalates rather than replying.

**Rationale**: this is the 169, 170 and 172 rule applied to a new surface: a
claim survives only when its evidence is verbatim in material the run actually
read. Those three specs each found the same class of model behaviour, an
answer that looks correct and cites something that does not exist, and each
found it in a live round rather than in review.

**Alternatives considered**: scoring the answer's similarity to the documents.
Rejected: every gate in this system is a rule with a yes or no answer, because a
threshold is a number somebody has to defend and a substring is not.

## D10. The curator's gate

**Decision**: the judgement's stated reason must quote text that appears in that
issue, and a qualifying issue is added to the backlog only.

**Rationale**: the same discipline, and the same reason the closer requires a
quote traceable to the thread it is closing. It also gives the human who reviews
the backlog something to check the judgement against.

## D11. The sensitive-keyword escalation

**Decision**: it stays a plain rule, evaluated inside the run before any model
call for that issue.

**Rationale**: it is deterministic, it is free, and it must not depend on a model
being available or well behaved. Spec 170 established the shape: fail closed
before any model call. Keeping it in coordinare was considered and rejected,
because coordinare no longer scans issues at all after this change, and splitting
one issue's handling across two processes to preserve one rule is worse than
moving the rule.

## D12. Which GitHub capabilities the performer gains

**Decision**: add two functions to the performer's GitHub module, one applying
labels and one adding an item to a project board, and add a field to `Score`
conveying the board's identifier.

**Rationale**: the performer can already post issue comments, and it already has
GraphQL plumbing with a per-dispatch endpoint override and existing call sites
to copy. It cannot label and it cannot add a board item; both exist only in
coordinare, and the board identifier reaches nothing in the performer today, so
without a new field the add-to-board call would silently no-op.

**Alternatives considered**: having coordinare perform the writes from the run's
report, the way wiki-init lets its service do the auto-merge. Rejected: it puts
coordinare back in the position of acting as the role, which is the thing this
feature exists to end.

## D13. What retires

**Decision**: delete `services/advocate.py`, `services/scoring.py`, the
`advocate_scan` node and its `START` edge, and the configuration fields only the
deleted path read. Their tests go with them.

**Rationale**: the constitution forbids dead code and the spec requires the old
path to be removed rather than left dormant, and two paths that can both answer
an issue is exactly how the persona and the code drifted apart in the first
place. Coverage is measured against the tree after deletion rather than assumed
to be unaffected.

## D14. The rate limit

**Decision**: a durable last-run timestamp per role and per repository, with a
cooldown that lengthens on repeated failure and a bounded attempt counter that
stops trying.

**Rationale**: the daemon poll is roughly every 30 seconds and a webhook can
shorten a cycle to nearly nothing, so "once per cycle" is not a rate limit.
The bootstrap gate already implements a persisted timestamp with an escalating
cooldown and a breaker, and there is no generic scheduler in this codebase to
reuse instead. An in-memory cooldown was rejected because it resets on restart,
and a restart loop would then mean a run every time.
