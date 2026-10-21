# Guided sharding: implementation plan

Implements [guided-sharding-amendment.md](guided-sharding-amendment.md), approved by the maintainer on
2026-10-21. Native execution, then one independent review. Builds on the sharded discovery code in
`environment_shard_scope.py`.

## Builder's calls (for maintainer review)

- **Two independent triages, both before authoring.** One author-side and one reviewer-side listing-only triage
  run on the same listing shards. A path is excluded only when both judge it `unrelated`; any disagreement means
  the path is authored. This gives the amendment's independent check on every exclusion without a separate
  contest round. Reviewers still see the exclusion record and can raise `unread_source` findings.
- **The exclusion record lives in `ScopeDefinition.exclusions`.** It is omitted when empty, so existing digests are
  unchanged, and it is covered by the reviewed definition digest and the accepted contract.
- **Listing shards reuse the planner** with listing cost (path length plus framing) as weight instead of file size.
- **A verdict on a directory covers its descendants**; the longest matching path wins. Every listed file must be
  covered by some verdict, which is a correctable error.

## Tasks

1. `read_file` and `read_files` ignore a stray `cursor` key (`SourceInspection.tool_result`).
   Test: `test_353_guided_tolerance.py`.
2. Models in `packages/.../environment/scope.py`:
   - `TriageVerdict(path, verdict: environment|unrelated|unsure, rationale ≤ 512 bytes)`;
   - `ListingTriageEnvelope{listing_triage: {verdicts ≤ 2048}}`;
   - `ScopeExclusion(path, files, rationale)`;
   - `ScopeDefinition.exclusions` (≤ 512, dropped when empty).
   Add the `listing_triage` response schema envelope.
3. `environment_shard_triage.py`:
   - `listing_shards(entries, share)`;
   - `triage(scope, side) -> dict[path, verdict]` (the pool, per-agent corrections, persisted per listing shard);
   - `exclusions(readable, spine, author, reviewer) -> (excluded paths, ScopeExclusion records)`.
4. Integrate into `ShardedScope.author_round`:
   - triage both sides;
   - author only shards restricted to non-excluded paths, while cross-references still route through the full
     plan (so a referenced excluded file is read);
   - the definition carries the exclusions.
   The review targets only shards with authored paths or retained findings. The merge reviewer receives the
   exclusions and is told to judge them.
5. Tests (`test_353_guided_sharding.py`):
   - both triages are required to exclude;
   - an uncovered path is a correctable error;
   - a referenced excluded file is authored;
   - exclusions are in the definition and reach the merge reviewer;
   - an empty exclusion list keeps old digests;
   - triage resume does not repeat work;
   - call counts scale with authored areas.
6. Full suite, independent review, preflight, push, CI, then pinned Elm and Dettmore on GLM.

## Review fixes and known limits (2026-10-21)

Fixed after independent review:
- A repeated path in one triage is a correctable error.
- A directory judged differently across listing shards keeps the more inclusive verdict.
- Every authored shard gets its first review even if its author read nothing.
- Exclusions are recomputed after a revision reads an excluded file.
- Exclusion records coarsen to stay within 512 with exact file counts. If even top-level areas exceed the
  bound, nothing is excluded.
- A definition that cannot fit holds as `scope_size_limit`.

Known limits:
- Both triage sides use the same model and prompt in separate conversations, so their agreement is independent
  judgment, not independent models.
- The merge reviewer's check of exclusions is an instruction, not an enforced finding.
- `ScopeExclusion` records the two sides' rationales but not an agent identity.
- A coarsened record names the containing area; its file count is exact.
