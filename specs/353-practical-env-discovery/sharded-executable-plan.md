# Sharded executable construction and review: implementation plan

Implements [sharded-executable-amendment.md](sharded-executable-amendment.md), approved in full by the maintainer
on 2026-10-22. Native execution with an independent review after each phase, then preflight and CI on #355, then
detached pinned Dettmore and Elm runs. All changes must be model-agnostic.

**Trigger:** Dettmore v3 accepted a sharded scope with 129 criteria, then held at `discovery_context_limit`,
because manifest construction preloads every scope input into one conversation.

## Protocol consequence to confirm

Two invariants stay as they are:
- a manifest binds every scope input by digest (`validate_scope_mapping`);
- the executable reviewer has read every manifest input (`_validated_assessment`).

What changes is what "read" means. The daemon fetches and verifies each bound input into the agent's inspection by
digest, but sends the conversation only an index: path, digest, size, and the criteria citing it. The agent reads
content on demand through the existing source tools, charged to the attempt budget. That is the amendment's
"evidence on demand, not preloaded". Reviewer independence is kept: reviewers get no author conversation, and the
daemon's verified reads are their own inspection.

## Phases

### Phase A: evidence on demand (single author and reviewer)

- `environment_discovery._discover_attempt`, `_initial_repair`, `_prepare_finding_context` and
  `_prepare_review_evidence` stop putting file contents into messages. A helper
  `bind_evidence(inspection, inputs) -> index` verifies and admits the bytes (charged reads), then returns
  `[{path, sha256, bytes, cited_by}]`.
- Instructions say the index is complete and bound, and the agent reads what it needs.
- Prompts keep criteria, citations, probe evidence, findings and the previous manifest.
- Tests: messages carry no file content; the manifest still binds every scope input; the reviewer's inputs still
  cover the manifest's; context is bounded for a 129-criterion scope.

### Phase B: global environment author and per-shard check authors

- **Ownership:** the sharded scope's work plan owns criteria (`criterion_owner`); merge-owned criteria belong to
  the global author.
- **Global author:** one conversation with probes. It writes the requirements, install, activation, services,
  runtime endpoints and local credentials, plus checks and `criterion_checks` for merge-owned criteria. It sees a
  bounded digest of every criterion (id, short description, applicability).
- **Check authors:** one per work shard, run in parallel. Each is given the global proposal's ids, activation and
  services, and writes `checks` plus `criterion_checks` for its shard's required criteria. Its check ids must start
  with its daemon-assigned prefix (`c<NN>-`), which gives unambiguous namespacing. Check authors may not declare
  install, services or activation.
- **Merge** (`merge_executable_parts`) is deterministic:
  - the global part plus the union of shard checks and mappings;
  - an id collision, a reference to an unknown service or check, or a criterion mapped by two parts is a
    correctable error returned to the responsible author, never resolved by picking one.
- **Validation:** the merged proposal goes through the existing `ReviewedConstruction.admit` unchanged.
- **Gating:** used only when the scope came from sharded discovery and has more criteria than one share holds;
  otherwise Phase A's single author runs.

### Phase C: divided executable review

- Shard reviewers judge their shard's checks against their criteria; a global reviewer judges install,
  activation and services, plus cross-cutting coverage.
- They combine into one coverage assessment per round, with the same ownership, capacity split and verdict rules
  as sharded source review (`combine_round`).
- Coverage revisions route each finding to the author that owns it.
- Execution repairs route a failed check to its owning shard author. Install, activation and service failures go
  to the global author, with the original failure evidence.

### Phase D: persistence and recovery

- The journal gains optional `executable_parts` (global proposal, shard proposals, prefixes) and per-round review
  parts.
- A restart resumes without renewing any budget.
- Validation rejects changed ownership or prefixes.

### Phase E: consolidation output budget (builder's call within the approved consolidation design)

- Consolidation chunks are bounded by criterion count (25), not only bytes, so a reply that restates groups fits
  any model's output cap.
- Trigger: Dettmore v3's consolidation replies hit the 32K output cap three times.

## Review focus

1. A required criterion left unmapped after merge; the merged manifest must fail validation, not pass.
2. A shard check author trying to declare install or services, or using another shard's prefix.
3. An execution failure in a shard check: it routes to that author, and the global part is preserved.
4. Restart mid-merge or mid-review: the counters are not renewed.
5. Phase A context: no file contents in any executable conversation unless the agent read them.

## Delivery

Phases A to E, each with tests and an independent review, then preflight, push to #355, CI, then detached
Dettmore, then Elm.
