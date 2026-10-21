# Sharded source discovery for repositories larger than one context

Issue #353; PR #354. Status: approved by the maintainer on 2026-10-21.

## Intent

Environment bootstrap must work on repositories whose relevant source does not fit
in one model conversation. The maintainer's self-hosted model
(`spark/glm-5.3-flash`: 262,144 input and 32,768 output tokens, up to 16 concurrent
requests) is the target, and running costs matter: the Anthropic usage limit that
stopped the latest attempts was set deliberately. Today source discovery is one
author conversation and one independent reviewer conversation per round, each
accumulating every inspected file; that forces a 1 MiB ceiling on GLM and has never
completed on Elm or Dettmore.

The approach is divide and conquer: split the pinned repository into pieces that
each fit one agent, let agents work in parallel and recursively, and pass bounded,
cited summaries upward instead of raw source.

Success: pinned Elm and Dettmore complete the full bootstrap (accepted source and
executable coverage, one manifest through installation and an offline clean
consumer, every required check and service step passing, confirmed cleanup).
After that, the wider repositories in the organization snapshot are tested.

## Goals

- No single model conversation needs more than its own share of the repository.
- Work divides by size, not by name, and rebalances while running.
- Recursion in both directions: a too-large share splits further; summaries too
  large to merge at once merge through intermediate levels.
- Shards run concurrently within the server's capacity.
- Very large files and symbolic links never stop an attempt.
- Independent review stays independent at every level.

## Non-goals

- No language, framework, file-type or repository-specific rules. Splitting uses
  only paths, byte sizes and modes from the pinned inventory.
- No change to the source review decision protocol (dispositions, derived status),
  the executable review, installation, the clean consumer or any readiness gate.
- No reading of very large files in byte ranges.
- No renewed or per-shard budgets beyond the attempt's single original budget.

## Anti-goals (failure even if it technically runs)

- An obligation present in a shard is lost or weakened by summarizing.
- Summaries replace citations: every criterion still cites files some agent
  actually inspected at the pinned commit.
- Recursion or parallelism that can run away, exceed the attempt budget, or leave
  work unaccounted for.
- Author and reviewer agents sharing conversations, caches or summaries in a way
  that compromises independent review.
- A file the policy refuses to read becomes a blocking gap that can never close.

## Decisions

### Inventory with size and mode

Discovery's pinned inventory carries each path's byte size and mode, taken from the
already pinned execution source identity, so splitting needs no file reads.

### Unreadable by policy

A file larger than the per-file read limit (existing default 256 KiB) or a symbolic
link is listed with its path, size and kind but never read. A read request for one
returns bounded evidence (`too_large` with its size, or `link` with its recorded
target text, never followed), not a hold and not an absence. A `source_gap` cannot
rest only on such a file: an `unread_source` gap must still request a readable
existing path. Main dependency declaration files are small and pass naturally;
large lockfiles are skipped, since installation tools read them when they run. The
rule names no file types.

### Size-based shards

Readable files are grouped into shards by walking the directory tree. A directory
whose readable bytes fit one agent's share becomes a shard; a larger one splits
into its children; small neighbours are packed together. One agent's share is a
fixed fraction of its model context (executor's call; about half, leaving room for
instructions, reasoning and output). Shards form a queue; a free agent takes the
next one.

### Shared spine

Small repository-root files (for example the top-level readme and build entry
points, identified only by being at the root and readable) are inspected once and
delivered to every agent as a cited digest, so each shard sees the project's
entry points without rereading them.

### Shard agents and recursion

Each author shard agent inspects its shard and returns a bounded summary: proposed
criteria with exact citations from its shard, open questions, and cross-references
to paths outside its shard. Cross-references become work for whichever shard owns
those paths, never reads by the agent that found them. An agent that discovers its
shard exceeds its share splits the remainder and returns the pieces to the queue.
Parents merge child summaries; if the combined summaries do not fit one context, an
intermediate merge level is added. Maximum depth is computed from the inventory
before work starts.

### Independent review, divided the same way

Fresh reviewer agents review shards with no access to author conversations or
author caches. They read their shard's cited files themselves. A merge-level
reviewer judges the merged scope for cross-shard omissions and conflicts using
reviewer summaries plus its own reads. The existing decision protocol (one
disposition per finding, daemon-derived status) applies at every level, and
findings name the shard that raised them.

### Concurrency and budget

Up to 12 agents run at once (executor's call, leaving headroom below the server's
16 for reviewers and merges). Every agent at every level draws from the attempt's
one budget of deadline, model calls and reads. Recursion and parallelism never add
allowances. For sharded attempts the maintainer set the budget to 1,024 model calls
and 8,192 reads, keeping the 3,600-second attempt deadline and 8 coverage revisions
shared across all shards. A phase timeout bounds each agent call, not the whole
sharded round (executor's call).

### Scope size

The merged scope may hold up to 512 criteria and the finding ledger up to 128
findings (maintainer decision), still bounded by their byte limits. A repository
needing more holds with an explicit scope-size code; obligations are never dropped
or folded to fit.

### Executor's calls on mechanics

- Shards are sized in bytes, at about half the model context, converting
  conservatively at 3.5 bytes per token. Each agent's own conversation stays within
  the existing per-call context check, so the 4 MiB per-inspection source ceiling
  applies per agent, not per repository.
- Persisted state holds only shard metadata, cited summaries and queue state, never
  source text, so the existing 4 MiB journal bound still applies.
- `too_large` and `link` are new bounded investigation statuses, each carrying an
  evidence digest like the existing statuses; reading tools return the same
  evidence instead of raising.
- One finding ledger per attempt. Each review round gathers every shard reviewer's
  and the merge reviewer's dispositions into a single review sequence; findings
  record the shard that raised them, and a merge-level review may disposition any
  retained finding.
- The final scope definition is assembled by the daemon from shard criteria
  (deduplicated by criterion identity), not regenerated in one model call, so model
  output limits cannot truncate it.

## Left to the builder

- Exact share fraction, summary size bound and their byte/token accounting.
- The summary format and the merge algorithm for criteria (deduplication by
  identity, preserving every required obligation).
- How shards, summaries and queue state persist for recovery within the existing
  journal and its bounds.
- How the merged scope maps onto the existing scope contract and finding ledger.

## Delivery

Maintainer approval of this design, then an implementation plan with tests for
splitting, recursion bounds, unreadable-by-policy evidence, independence and
budget accounting, then implementation, independent review, preflight and CI.
Then pinned Elm and Dettmore on `spark/glm-5.3-flash`, then the wider organization
repositories.
