# Data Model: Stage Verdict Memory (125)

Snapshot schema **v13 → v14**. All additions are backward-compatible defaults; pre-v13 snapshots load with empty caches and byte-identical behaviour (FR-011/FR-012).

## StageVerdict (new pydantic submodel, `state_store.py`)

| Field | Type | Semantics |
|---|---|---|
| `head_sha` | `str` (non-empty) | PR head commit the stage's passing verdict was issued against (performer-reported settled head) |
| `verdict` | `str` | the passing terminal marker recorded (`approved`, `security_passed`, `qa_passed`, `docs_committed`) |
| `recorded_at` | `str` (ISO-8601 UTC) | observability only — **never** consulted by skip decisions (spec Edge Cases: SHAs, not times) |

`model_config = ConfigDict(extra="forbid")`. Validation: `head_sha` and `verdict` must be non-empty (a record that cannot gate anything is never written).

## PersistedSession (per-card) — new field

| Field | Type | Default | Semantics |
|---|---|---|---|
| `stage_verdicts` | `dict[str, StageVerdict]` | `{}` | single slot per verdict stage (`reviewing`, `security`, `qa`, `documenting`, `closing_review`); a new passing verdict for a stage **overwrites** the old slot. Never holds entries for `implementing`/`assessing` (FR-004). |

Derived value: **last documented SHA** := `stage_verdicts["documenting"].head_sha` (US2/FR-006-007 — no separate field).

Plumbing parity (follows `bounce_counter`/`local_fix_counter` precedent): mirrored on `CardSession` (`session.py`) + `_SESSION_FIELDS`, persisted by `daemon._persist_active_sessions`, restored by `daemon._restore_from_snapshot` into per-card session state, surfaced in `CoordinareState` for the dispatch node.

## PersistedSession (per-card) — comment watermark fields

**Correction (2026-07-04, adversarial review):** the comment router (`route_issue_comments`) reads the ACTIVE card's linked issue, and both keys already round-trip session ↔ state via `_SESSION_FIELDS` — so persistence is **per-card on `PersistedSession`**, not top-level.

| Field | Type | Default | Semantics |
|---|---|---|---|
| `processed_issue_comment_ids` | `list[int]` | `[]` | already-classified issue-comment IDs for this card's issue; persisted **bounded**: the numerically largest 2000 (GitHub comment IDs are monotonic → largest = newest). Restored as `set[int]` into the session. |
| `last_issue_comment_id` | `int \| None` | `None` | the `since_id` fetch watermark (`route_issue_comments.py:41`); restored verbatim. |

## Transient state (not persisted)

| Key | Type | Semantics |
|---|---|---|
| `override_forced_dispatch` | `str \| None` on `CoordinareState` | one-shot stage name set by `_apply_pending_override` `restart`; consumed (cleared) by the verdict-cache check to guarantee an operator-restarted stage always dispatches (US1 scenario 4). Deliberately not persisted: a crash loses only the forcing, never a verdict. |

## State transitions

```
verdict stage completes with its matching passing marker + resolvable head
    └─> stage_verdicts[stage] = StageVerdict(head_sha, verdict, now)      [monitor_performer, pre-_advance_stage]

dispatch evaluation for verdict stage S:
    override_forced_dispatch == S ─────────────► dispatch (flag cleared)
    relay_feedback non-empty ──────────────────► dispatch
    stage_verdicts[S] missing ─────────────────► dispatch
    live head unresolvable (API error) ────────► dispatch                  [FR-003 fail-open]
    stage_verdicts[S].verdict != expected(S) ──► dispatch
    stage_verdicts[S].head_sha != live head ───► dispatch
    else ──────────────────────────────────────► skip: _advance_stage +
                                                  log stage_skipped/verdict_cached

documenting stage additionally (only reached when the cache did not skip):
    stage_verdicts["documenting"] exists:
        compare(S_doc...live_head) touches no docs/ path ─► skip: no_doc_changes_since_last_pass
        compare fails/capped ────────────────────────────► fall back to 123 whole-PR gate
    no prior doc pass ───────────────────────────────────► 123 whole-PR gate (unchanged)
    123 gate fetch fails ────────────────────────────────► dispatch        [FR-008]
```

## Invalidation

- **Any new head** (commit, rebase, force-push) ⇒ SHA mismatch ⇒ miss. No explicit invalidation calls needed.
- **New passing verdict** for a stage ⇒ slot overwrite.
- **Not invalidated** by persona/model/config changes (spec Assumptions; operator override is the escape hatch).
- Records are per-card; card completion discards the session (existing lifecycle), so no cross-card leakage is possible.
