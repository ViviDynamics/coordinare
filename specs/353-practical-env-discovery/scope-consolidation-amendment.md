# Scope consolidation and focused review navigation

Issue #353; PR #355. Status: written proposal for maintainer review. Amends
[file-level-triage-amendment.md](file-level-triage-amendment.md).

## Why

The pinned Dettmore attempt `source-evidence-cov8-internal-glm53flash-dettmore-v1` (head be4787b9) was the
first sharded run to reach independent source review cleanly, with 115 calls and no schema corrections. It was then
cut off by the operator tool's two-hour background limit. Two problems showed:

1. **Over-fine scope.** Reference-following from the named files produced 190 criteria, 188 of them required.
   They are rarely duplicates. Instead they describe tooling internals as environment obligations: one criterion
   per agent skill script (`check-wiring`, `quality-guard`, `_lib.sh` helpers, token lookup order). Every required
   criterion must later map to a passing executable check, so an over-fine scope makes review slow and the manifest
   stage much harder, on any model.
2. **Wandering review.** Shard and merge reviewers can navigate the whole repository. They spent 37 of 53 tool calls
   listing directories rather than reading cited evidence.

## Goals

- Criteria state what the development and test environment must provide, or which documented operations must
  succeed, at the level a person bootstrapping the repository would check.
- No obligation is lost, no citation is dropped, and no applicability is downgraded.
- Reviewers spend their calls reading evidence, not browsing.
- Every change is model-agnostic: guidance, envelopes and daemon rules, never a model-specific patch.

## Decisions (proposed)

### 1. Obligation granularity guidance (all authors, single and sharded)

Author and revision instructions state:
- A criterion is an environment obligation: a tool or service the environment must provide, a dependency set that
  must install, or a documented development or test operation that must succeed, with its observable result.
- The internal behaviour of a script is evidence for an obligation, not an obligation itself.
- Related steps of one documented operation form one criterion.

This applies equally to the single-conversation path.

### 2. A consolidation step after authoring

When the merged shard summaries exceed a threshold (proposed: 40 criteria), one consolidation agent receives:
- the complete criteria list, with ids, descriptions, applicability and sources;
- no file contents;
- the granularity rule above.

It returns `groups`. Each group lists the member criterion ids and one consolidated criterion proposal.

The daemon applies a group only when its consolidated criterion:
- cites the union of the members' sources (at most 32; a larger group is rejected as a correctable error asking
  for smaller groups);
- has the strongest member applicability;
- names no source outside the members' sources.

Ungrouped criteria are kept unchanged. The consolidation record (group to member ids) goes in the journal and the
definition, so reviewers see what was folded. If the consolidation agent fails, the unconsolidated scope proceeds;
the step can only shrink a scope, never block it.

Independent reviewers then judge the consolidated definition as usual. A member obligation lost in a merge is a
`missing_obligation` finding against the folded criterion, and the revision path restores it. When the scope is
too large for one agent's share, consolidation runs per group of shards and then once more across the result.

### 3. Focused review navigation

Shard and merge reviewers receive the list of files their criteria cite and the files referenced from their evidence.
Their instructions say to read from those lists and to use `list_files` only to resolve a concrete reference that
is not listed.

Navigation stays available, because a reviewer must still be able to find omissions, but it is no longer the
default way to explore. The per-conversation read allowance and the spent-allowance message are unchanged.

### 4. Detached live runs

Validation runs are launched detached from the operator tool, so its two-hour background limit can't cut off a
longer approved attempt. This is a run procedure only; the product is unchanged.

## Non-goals

- No change to the decision protocol, finding ledger, contract, journal invariants or acceptance gates.
- No filename, language or framework rules.
- Consolidation is not a reviewer and never closes findings.

## Delivery

Maintainer approval, a short plan, native implementation with independent review, preflight and CI on #355,
then a detached rerun of Dettmore, then Elm.

## Review fixes (2026-10-21)

- Consolidation never blocks: its own phase deadline, call limit or context overflow leaves the scope as authored. Only the attempt's budget or deadline still ends the attempt.
- A group must carry exactly the union of its members' runtime needs, as well as their sources and their strongest applicability.
- The merge reviewer receives the consolidation record, with each folded member's description.
- A record whose criterion a revision replaced is dropped.
- A listing-only agent's tool request is corrected, never executed.
- Builder's call: one chunked consolidation pass, without the cross-chunk second pass. Obligations split across chunks stay unfolded, and nothing is lost. A restart before the definition is written reruns consolidation.
