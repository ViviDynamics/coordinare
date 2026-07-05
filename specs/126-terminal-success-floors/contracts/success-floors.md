# Contract: terminal-success floors, feedback ledger, dispositions (126)

Consumed by `tests/unit/graph/nodes/test_success_floor.py`, `test_feedback_ledger.py`, `test_documenting_noop_completion.py`.

## Ledger stamping (bounce sites)

| # | Event | Required effect |
|---|---|---|
| L1 | Any bounce that queues implementer-bound feedback (reviewer `changes_requested`, CI-gate bounce, `security_failed`, `qa_failed`) | Each item gets a stable id (`fb-<n>`, monotonic per card) + `raiser` (stage name, or `ci` for CI-gate items); `feedback_origin_sha` set to the head the verdict was raised against; ledger records appended (body digested ≤200 chars); relay entries carry `id`/`raiser`/`re_raised` inline |
| L2 | New feedback round stamped | prior round's open records marked superseded and pruned beyond 1 prior round; `noop_success_retries` reset to 0 ONLY when the origin head differs from the prior round's (a cross-raiser bounce at the SAME head is not progress and must not re-arm the F4 retry — adversarial-review fix) |
| L3 | Origin head unresolvable at bounce time | items stamped with `origin_sha=""` — the floor then fails open (F5) |

## Implementer success floor (implementing terminal success, before the CI gate)

| # | Condition (in order) | Outcome |
|---|---|---|
| F1 | No `feedback_origin_sha` (first-pass dispatch, or L3) | accept — today's behaviour (fail-open, FR-011) |
| F2 | `head_after` differs from `feedback_origin_sha` | accept; dispositions (if any) recorded onto ledger; missing dispositions ⇒ items stay `open` and ride forward |
| F3 | head unmoved AND ≥1 `disputed` disposition | accept completion; disputed items queue for their raiser (D-rows); `addressed` claims on an unmoved head are recorded but items stay open |
| F4 | head unmoved AND zero disputed AND `noop_success_retries == 0` | REJECT: strengthened re-dispatch (relay = unaddressed items + directive), `noop_success_retries = 1`; NO content/transient budget increment; log `monitor_performer.success_floor_retry` |
| F5 | head unmoved AND zero disputed AND `noop_success_retries >= 1` | HOLD: `phase="blocked"`, open_questions name the unmoved head + outstanding ids; log `monitor_performer.success_floor_hold` |
| F6 | `head_after` missing/unresolvable on the completion | accept (fail-open, FR-011) |
| F7 | Stage not `implementing` | floor never evaluated (verdict roles exempt, FR-009; documenting has its own U-rows) |

## Dispute adjudication

| # | Condition | Outcome |
|---|---|---|
| D1 | Disputed items with raiser ∈ {reviewing, security, qa, closing_review} | queued; that stage's next dispatch context includes `disputed_feedback` [{id, body, reason}] |
| D2 | Raising stage passes while its disputes pending | round accepted: disputed entries closed (`dispute_accepted`) |
| D3 | Raising stage bounces again while its disputes pending | round rejected: disputed entries closed (`dispute_rejected`); the NEW round's items stamped `re_raised=true` |
| D4 | Completion disputes items of a `re_raised` round with head unmoved | HOLD (no second automatic lap, FR-006) |
| D5 | Disputed item with `raiser == "ci"` | HOLD directly (CI cannot adjudicate, FR-007) |
| D6 | Pending disputed entries for stage S | 125 verdict-cache skip for S is vetoed (dispatch normally) — FR-012 |

## Documenting no-op completion (US3)

| # | Condition | Outcome |
|---|---|---|
| U1 | `docs_committed` with ≥1 `files_modified` OR a head delta | today's behaviour; 125 records the documentation pass |
| U2 | `docs_committed` with zero `files_modified` AND no head delta | advance stage; emit `documenting_noop_completion`; 125's verdict record SUPPRESSED (no documentation pass minted); never bounced |

## Invariants

- I1: floors only ever fire on terminal-success markers; blocked/partial_progress keep their 070/072 handling untouched.
- I2: no floor path increments `content_feedback_cycles` or `transient_error_cycles`.
- I3: with an empty ledger and no origin SHA, every row reduces to today's behaviour (regression baseline, SC-006).
- I4: dispositions for unknown ids are ignored (logged); an id can only be disposed once per round.
- I5: all floor decisions emit structured events (`success_floor_retry`, `success_floor_hold`, `dispute_queued`, `dispute_accepted`, `dispute_rejected`, `documenting_noop_completion`).

## Wire contract

- Dispatch: relay_feedback entries MAY carry `id: str`, `raiser: str`, `re_raised: bool`; new optional card_context field `disputed_feedback: list[{id, body, reason}]` (registered in `specs/contracts/dispatch-payload.md`).
- Response: `ProtocolResponse.feedback_dispositions: list[{id: str, disposition: "addressed"|"disputed", reason?: str}]`, default `[]`, mirrored on both coordinare and performer models; absent ⇒ nothing disputed.
- Persona: implementer instructions require a disposition per delivered feedback id, with `disputed` requiring a reason.
