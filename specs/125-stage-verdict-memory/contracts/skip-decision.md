# Contract: verdict-cache skip + documenting gate decisions (125)

Behavioural contract consumed by `tests/unit/graph/nodes/test_dispatch_verdict_cache.py` and `test_dispatch_doc_gate_sha.py`.

## Constants

- `VERDICT_STAGES = {reviewing, security, qa, documenting, closing_review}`
- `EXPECTED_MARKER = {reviewing: approved, security: security_passed, qa: qa_passed, documenting: docs_committed, closing_review: approved}`
- Never gated: `implementing`, `assessing`, `architecting`, `env_bootstrap` (FR-004).

## Verdict-cache decision table (any stage in VERDICT_STAGES)

| # | Condition (evaluated in order) | Outcome |
|---|---|---|
| V1 | `override_forced_dispatch == stage` | dispatch; clear the flag |
| V2 | `relay_feedback` non-empty | dispatch |
| V3 | no `stage_verdicts[stage]` record | dispatch |
| V4 | `pr_node_id` missing OR `check_mergeability` raises OR returns empty `head_ref_oid` | dispatch (fail-open, FR-003) |
| V5 | `stage_verdicts[stage].verdict != EXPECTED_MARKER[stage]` | dispatch |
| V6 | `stage_verdicts[stage].head_sha != live head_ref_oid` | dispatch |
| V7 | all of V1–V6 pass their negations | **skip**: `_advance_stage`; log `dispatch_performer.stage_skipped` `{reason: "verdict_cached", card_id, performer_stage, head_sha}` |

## Recording rules (monitor_performer, terminal-success block)

| # | Condition | Outcome |
|---|---|---|
| R1 | `stage ∈ VERDICT_STAGES` AND `marker == EXPECTED_MARKER[stage]` AND settled head resolvable (`status.head_after` else `status.head_sha`, non-empty) | write `stage_verdicts[stage] = {head_sha, verdict: marker, recorded_at}` (overwrite) |
| R2 | marker matches but no resolvable head | no record; log debug; advancement unaffected |
| R3 | stage not in VERDICT_STAGES (implementing/assessing) or marker mismatch for stage | no record |
| R4 | stage advanced via persona-scope skip, override skip, or verdict-cache skip | no record (nothing was verified) |
| R5 | terminal success tainted by `env_cache_health_failed` | no record — the taint holds advancement so the stage genuinely re-runs; recording would let the verdict cache skip exactly that re-run (adversarial-review clarification) |

## Documenting gate (runs only if V7 did not skip; replaces the 123 call site)

| # | Condition | Outcome |
|---|---|---|
| D0 | `relay_feedback` non-empty | dispatch (queued feedback is explicit work — stricter than the 123 baseline) |
| D1 | `stage_verdicts["documenting"]` exists AND `compare_changed_files(S_doc, live_head)` succeeds AND no returned path starts with `docs/` | **skip**: `_advance_stage`; log `stage_skipped` `{reason: "no_doc_changes_since_last_pass"}` |
| D2 | compare succeeds AND ≥1 path starts with `docs/` | dispatch tech_writer |
| D3 | compare raises / capped (>300 files) / live head unavailable | fall back to D4 |
| D4 | no prior doc pass OR fallback from D3: spec-123 whole-PR gate (`_should_skip_documenting` on shared-fetch `changed_files`) | skip (`no_doc_changes`) / dispatch per 123 semantics |
| D5 | shared PR-diff fetch also fails | dispatch (never skip on an unknown diff, FR-008) |

## Shared fetch (US3)

- F1: within one `_dispatch_performer_body` evaluation, `github.get_pr_diff` is called **at most once**; the doc gate's `changed_files` and the diff-injection's raw text come from that single call.
- F2: fetch failure ⇒ gate treats `changed_files` as unknown (D5) AND injection omits the inline diff — identical to today's two independent failure paths.

## GitHub service addition

`compare_changed_files(pr_url: str, base_sha: str, head_sha: str) -> list[str]`
- REST `GET {api}/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}` (JSON), collecting `files[].filename`; paginates `per_page=100` up to 3 pages, then raises `RuntimeError("compare truncated")` (→ D3).
- Raises on malformed URL / missing token / non-2xx (→ D3). Never logs the token; logs only counts.

## Comment watermark (US4)

- W1: snapshot save persists `sorted(processed_issue_comment_ids)[-2000:]` and `last_issue_comment_id`.
- W2: restore populates `CoordinareState.processed_issue_comment_ids` as `set[int]` and `last_issue_comment_id` verbatim; missing fields (pre-v13) ⇒ empty set / None.
- W3: `route_issue_comments` behaviour is unchanged in-code; its dedup now survives restart.

## Invariants

- N1: a skip NEVER occurs when the live head cannot be positively resolved.
- N2: with `stage_verdicts == {}` and no persisted watermark, every decision table row reduces to "dispatch"/123 semantics — byte-identical baseline (FR-012).
- N3: skip events are distinguishable: `verdict_cached` vs `no_doc_changes_since_last_pass` vs 123's `no_doc_changes` (FR-005).
- N4: verdict records never exist for `implementing`/`assessing` (enforced at write, R3).
