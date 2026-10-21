# Sharded Source Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or superpowers:subagent-driven-development if the maintainer chooses it) to implement this plan task-by-task. Steps use checkbox tracking.

**Goal:** Let source discovery and independent source review work on repositories larger than one model context by dividing the pinned repository into size-based shards processed recursively and in parallel, while very large files and symlinks never stop an attempt.

**Architecture:** Five phases, each shippable on its own. Phase 1 (inventory metadata, unreadable-by-policy evidence, enlarged bounds) also fixes the Dettmore symlink hold. Phases 2-3 add a shard planner, a bounded parallel agent pool, shard authoring with cited summaries and a deterministic merge, then sharded independent review feeding the existing single finding ledger. Phase 4 persists shard state for recovery. Phase 5 wires the harness and runs pinned Elm and Dettmore on `onprem/glm-5.3-flash`.

**Tech Stack:** Existing Python 3.12+, asyncio, Pydantic v2. No new dependency.

**Spec:** [sharded-discovery-amendment.md](sharded-discovery-amendment.md), approved by the maintainer on 2026-10-21. This plan was approved by the maintainer on 2026-10-21, together with a request for an executable-stage design ([sharded-executable-amendment.md](sharded-executable-amendment.md)).

## Global Constraints

- Splitting uses only paths, byte sizes and modes from the pinned inventory; no language, framework, file-type or repository-specific rules.
- Per-file read limit stays `max_file_bytes` (256 KiB default); larger files and symlinks (mode `120000`) are listed, never read, never followed.
- Agent share: about half the model context, converted at 3.5 bytes per token; every agent conversation still passes `check_evidence_context`.
- Concurrency: at most 12 agents at once.
- Sharded budget (maintainer): 1,024 model calls, 8,192 reads, 3,600-second attempt deadline, 8 coverage revisions shared across all shards; recursion and parallelism never add allowances.
- Scope caps (maintainer): 512 criteria, 128 findings; obligations are never dropped or folded; overflow holds with `scope_size_limit`.
- Decision protocol, executable review, installation, clean consumer and readiness gates unchanged.
- Independent reviewers never see author conversations, caches or summaries.

## Plan rulings for maintainer review

- **Byte bounds scale with the count caps** (the design kept byte limits implicit): scope definition 1 MiB, scope contract 2 MiB, finding ledger 512 KiB, manifest document 4 MiB, repair journal 16 MiB. The design's executor note that the 4 MiB journal bound "still applies" cannot hold once a 512-criterion definition is persisted alongside its contract.
- **Open item outside the approved design:** executable manifest construction and executable review still load all scope inputs into one conversation (`environment_discovery._discover_attempt`, `prepare_mandatory_evidence(..., scope.all_inputs)`). On a repository whose sharded scope succeeds, that stage would hit the same context wall. This plan does not change it; a follow-up design is needed before readiness can be reached on large repositories.

## Maintainer rulings during Phase 2 (2026-10-21)

- Schema repairs: each shard agent has its own allowance of 2 corrections; an agent that exhausts it holds the attempt with `attempt_schema_limit`. The single-conversation path keeps the attempt-wide 8. Calls (1,024), reads (8,192), coverage revisions (8) and the deadline remain shared.
- Concurrency stays at 12 agents.
- Trigger: the GLM Elm attempt `...-creds-v3` held with `scope_revision_context_limit` (1.8 MB of retained evidence against the 1 MiB conversation cap).

## Review fix pass (2026-10-21)

The maintainer approved fixing every Critical and Important finding of the Phase 2-5 review, plus inexpensive minors:

- Cross-references are routed until none remain, recorded only after their follow-ups finish, under their own id namespace.
- Root-level (merge-owned) criteria reach the merge reviewer in full; the criterion index carries ids, short descriptions and applicability only.
- Review re-runs compare complete criterion content, so a rationale-only reassessment is reviewed again.
- Reviewers read their own evidence (shard inputs, spine, their findings' investigations) over the full inventory, and receive investigation evidence.
- Shards are bounded by half the per-conversation read allowance as well as by bytes; overflow splits by bytes.
- An accepted sharded scope resumes with daemon verification limits sized to its inputs.
- Revisions combine identical additions and follow reassessed IDs in responses; true conflicts hold with `scope_revision_conflict`.
- Revision agents receive only the criteria their findings name, so splitting findings shrinks evidence.
- Each review part reports at most its share of the ledger's remaining finding capacity (`review_findings_limit` correction).
- Shard summaries merge by exact criterion identity only; distinct obligations are never folded and citations never dropped.
- Sharded configuration defaults to the ruled budget (1,024 calls, 8,192 reads, 3,600 s) unless a value is set.

Known limits, not addressed here: review and merge conversations do not split on overflow (they hold with
`shard_context_limit`); inputs read by an agent that later overflowed stay in the scope inputs; shard questions are
not yet surfaced to reviewers; the contract's 2 MiB bound caps an accepted scope at roughly 3,000 inspected files;
executable construction still preloads every scope input (see sharded-executable-amendment.md).

## Review Focus

1. A directory with one huge readable file and many tiny ones: the huge file becomes its own shard, tiny files pack together. Test in Task 3.
2. A cross-reference cycle between two shards (A points into B, B into A): each path is assigned once, no infinite requeue. Test in Task 5.
3. A shard agent that fails or hits its context limit mid-run: its shard returns to the queue split, its spent calls/reads stay spent. Test in Task 4.
4. Two shards proposing the same criterion: the merge keeps one, preserving both citation sets; a required criterion is never downgraded. Test in Task 5.
5. Budget exhaustion while 12 agents are in flight: all are cancelled, the attempt holds once with the budget code, no partial scope is published. Test in Task 4.

---

## Phase 1: Inventory metadata, unreadable-by-policy evidence, enlarged bounds

### Task 1: Inventory entries and unreadable-by-policy reads

**Files:** Modify `src/coordinare/services/environment_inspection.py` (`SourceInspection`), `src/coordinare/services/environment_scope_investigation.py` (`_request_evidence`, `_bound_result`), `packages/service_inference/src/coordinare_service_inference/environment/scope_investigation.py` (`ScopeInspectionResult.status`), `src/coordinare/services/env_cache.py` and `scripts/validate_environment_repositories.py` (pass entries from the pinned execution source). Create `tests/unit/test_353_unreadable_by_policy.py`.

**Interfaces (produced):**
- `InventoryEntry(path: str, size: int, mode: Literal['100644','100755','120000'])` (frozen dataclass) in `environment_inspection.py`.
- `SourceInspection(..., entries: Mapping[str, InventoryEntry] | None = None)`; `SourceInspection.policy_status(path) -> Literal['readable','too_large','link']`.
- Reads of a `too_large` or `link` path return `{'path', 'status', 'size'}` (and `'target'` text for links) without fetching, reserving reads, or raising; `ScopeInspectionResult.status` adds `'too_large'` and `'link'` with an evidence digest.
- `_request_rule_errors`: an `unread_source` gap whose only existing-path requests are `too_large`/`link` is rejected like an absent-only request.

- [ ] **Step 1: RED.** `test_symlink_request_returns_link_evidence_without_hold` (Dettmore shape: `.opencode/skill` mode `120000`), `test_oversized_file_returns_too_large_without_read_charge`, `test_unread_source_gap_cannot_rest_on_unreadable_file`, `test_readable_paths_unchanged`, `test_entries_come_from_pinned_execution_source` (sizes/modes equal the `SourceEntry` list).
- [ ] **Step 2: Run RED.** `uv run pytest -q tests/unit/test_353_unreadable_by_policy.py`.
- [ ] **Step 3: Implement.** Entries default to `None` (every path readable, today's behavior) so existing callers keep working.
- [ ] **Step 4: GREEN** plus `test_353_scope_investigation_*`, `test_353_source_absence_*`, `test_353_inspection.py`.
- [ ] **Step 5: Commit** `feat: answer oversized files and symlinks with bounded evidence instead of holding`.

### Task 2: Enlarged count and byte bounds

**Files:** `scope.py` (criteria 512, definition 1 MiB, contract 2 MiB), `findings.py` (records 128, ledger 512 KiB), `environment_findings.py` (`MAX_FINDINGS = 128`), `models.py` (`MAX_DOCUMENT_BYTES = 4 MiB`), `environment_repair.py` (`JOURNAL_MAX_BYTES = 16 MiB`), `scope.py` investigation history bound unchanged. Test: `tests/unit/test_353_scope_bounds.py`.

- [ ] **Step 1: RED.** `test_scope_accepts_512_criteria_and_rejects_513`, `test_ledger_accepts_128_findings`, `test_overflow_holds_with_scope_size_limit` (no criterion dropped), `test_large_contract_fits_manifest_and_journal` (512 realistic criteria round-trip through manifest and journal).
- [ ] **Step 2: Run RED. Step 3: Implement** constants and the `scope_size_limit` hold. **Step 4: GREEN** plus `test_353_source_decision_*`, `test_353_scope_contract.py`, `test_353_review_findings_*`. **Step 5: Commit** `feat: allow 512-criterion scopes within enlarged byte bounds`.

## Phase 2: Shard planning and sharded authoring

### Task 3: Shard planner

**Files:** Create `src/coordinare/services/environment_shards.py`. Test: `tests/unit/test_353_shard_planner.py`.

**Interfaces:**
- `Shard(id: str, paths: tuple[str, ...], readable_bytes: int, depth: int)` (frozen).
- `share_bytes(context_tokens: int) -> int` = `int(context_tokens * 0.5 * 3.5)`.
- `plan_shards(entries: Mapping[str, InventoryEntry], *, share: int, spine: Collection[str]) -> list[Shard]`: walk the directory tree; a directory whose readable bytes fit `share` is one shard; larger directories split into children; adjacent small siblings pack into one shard up to `share`; a single readable file larger than `share` cannot occur (bounded by `max_file_bytes`) and is asserted. Deterministic order.
- `spine_paths(entries, *, limit_bytes: int) -> list[str]`: readable root-level files, smallest first, within `limit_bytes`.
- `max_depth(entries, *, share) -> int`.

- [ ] **Step 1: RED.** `test_shards_cover_every_readable_path_exactly_once`, `test_shards_respect_share`, `test_large_directory_splits_recursively`, `test_small_siblings_pack`, `test_one_big_file_among_many_small` (Focus 1), `test_unreadable_paths_listed_not_sharded`, `test_plan_is_deterministic`, `test_spine_is_root_only_and_bounded`.
- [ ] **Steps 2-5:** RED, implement, GREEN, commit `feat: plan size-based source shards from the pinned inventory`.

### Task 4: Bounded parallel agent pool

**Files:** Create `src/coordinare/services/environment_agent_pool.py`. Test: `tests/unit/test_353_agent_pool.py`.

**Interfaces:**
- `async run_pool(work: Sequence[T], worker: Callable[[T], Awaitable[R]], *, budget: AttemptBudget, concurrency: int = 12, split: Callable[[T, ShardOverflow], Sequence[T]]) -> list[R]`: a queue; at most `concurrency` workers; a worker raising `ShardOverflow` re-queues `split(item, exc)`; any other failure cancels all workers and re-raises once; every call/read charges the shared `budget`.
- `class ShardOverflow(Exception)` carrying the unprocessed remainder.

- [ ] **Step 1: RED.** `test_never_exceeds_concurrency`, `test_overflow_requeues_split_remainder` (Focus 3), `test_budget_exhaustion_cancels_all_and_raises_once` (Focus 5), `test_results_order_is_deterministic`, `test_no_new_allowances`.
- [ ] **Steps 2-5:** commit `feat: run shard agents in a bounded pool on the shared attempt budget`.

### Task 5: Shard authoring, cross-references and merge

**Files:** Create `src/coordinare/services/environment_shard_author.py`; modify `environment_scope.py` (`_discover_scope` chooses sharded authoring when `entries` and a context size are provided). Shared models in `packages/.../environment/scope.py`. Test: `tests/unit/test_353_shard_author.py`.

**Interfaces:**
- `ShardSummary(FindingEvidence)`: `shard_id`, `criteria: list[ScopeCriterionProposal]` (each citing only paths read by that agent), `cross_references: list[str]` (inventory paths outside the shard, max 64), `questions: list[str]` (max 16, 2048 bytes each), bounded to `summary_max_bytes`.
- `async author_shard(shard, *, spine_digest, chat, inspection_factory, budget) -> ShardSummary`: one agent conversation limited to its shard's paths plus the spine digest; source tools restricted to shard paths; raises `ShardOverflow` with the unread remainder when its context check would fail.
- `route_cross_references(summaries, shards) -> list[Shard]`: each referenced path is assigned once to the owning shard's follow-up work; already-covered paths are ignored (Focus 2).
- `merge_summaries(summaries) -> list[ScopeCriterionProposal]`: deterministic; duplicates by `criterion_id` keep one with the union of sources; `required` never downgraded (Focus 4); more than 512 criteria raises `scope_size_limit`. Merge levels are added when combined summaries exceed one context.
- The daemon builds `ScopeDefinition` from the merged criteria; no single model call regenerates the full scope.

- [ ] **Step 1: RED.** `test_agent_reads_only_its_shard`, `test_citations_only_from_inspected_paths`, `test_cross_reference_routed_once` (Focus 2), `test_duplicate_criteria_merge_without_downgrade` (Focus 4), `test_merge_levels_added_for_large_summaries`, `test_sharded_definition_round_trips_into_existing_contract`, `test_single_conversation_path_unchanged_without_entries`.
- [ ] **Steps 2-5:** commit `feat: author source scope in parallel shards with cited summaries`.

## Phase 3: Sharded independent review

### Task 6: Shard reviewers and merge-level review into one ledger

**Files:** Create `src/coordinare/services/environment_shard_review.py`; modify `environment_scope.py` review loop. Test: `tests/unit/test_353_shard_review.py`.

**Interfaces:**
- `async review_shard(shard, *, definition_slice, chat, inspection_factory, budget) -> ScopeAssessmentProposal`: fresh inspection, no author cache, criteria limited to those citing the shard.
- `async review_merge(definition, shard_assessments, *, chat, inspection_factory, budget) -> ScopeAssessmentProposal`: judges cross-shard omissions/conflicts with its own reads.
- `combine_round(shard_assessments, merge_assessment) -> ScopeAssessmentProposal`: one review sequence; every retained finding gets exactly one disposition (merge-level wins on conflict); new findings record `shard_id`; verdict accept only when every part accepts.
- Revision rounds re-run only shards with open findings plus the merge review; coverage revisions remain the shared count of 8.

- [ ] **Step 1: RED.** `test_reviewers_never_receive_author_material`, `test_round_yields_one_review_sequence`, `test_merge_disposition_wins_conflicts`, `test_accept_requires_every_part`, `test_only_open_shards_rerun`, `test_coverage_revisions_shared`.
- [ ] **Steps 2-5:** commit `feat: review sharded scope independently into one finding ledger`.

## Phase 4: Persistence and recovery

### Task 7: Persist shard plan, summaries and queue

**Files:** `environment_attempt.py`, `environment_repair.py` (journal fields `shard_plan`, `shard_summaries`, `shard_queue`; validation on restore). Test: `tests/unit/test_353_shard_recovery.py`.

- [ ] **Step 1: RED.** `test_roundtrip_preserves_shards_summaries_and_counters`, `test_restore_rejects_changed_inventory_or_plan`, `test_interrupted_shard_requeued_without_renewal`, `test_journal_stays_within_16_mib_for_512_criteria`.
- [ ] **Steps 2-5:** commit `feat: recover sharded discovery without renewing budgets`.

## Phase 5: Harness and live validation

### Task 8: Harness flags, review, CI and live runs

- [ ] **Step 1:** `scripts/validate_environment_repositories.py`: `--context-tokens`, `--max-model-calls`, `--agent-concurrency`; tests in `tests/unit/test_353_validation_harness.py`.
- [ ] **Step 2:** Focused suites, mypy, ruff, baseline; independent review of the branch with this plan's Review Focus; Critical or Important findings come to the maintainer.
- [ ] **Step 3:** Exact-head preflight, quality guard, push, PR body, CI.
- [ ] **Step 4:** Labelled pinned Elm then Dettmore runs on `onprem/glm-5.3-flash` via the internal endpoint with the sharded budget. Readiness only with every acceptance gate; the executable-stage open item above is expected to need its own design if source review succeeds.
