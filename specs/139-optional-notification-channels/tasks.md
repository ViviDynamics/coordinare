# Tasks: Notification Channels Fully Optional

**Feature**: 139-optional-notification-channels | **Issue**: #187 | **Branch**: `139-optional-notification-channels`

**Scope guardrails**: do not change delivery behaviour; do not relax validation for any subsystem
other than notifications; `config validate` must keep reporting these as errors; no push or PR
without approval.

## Phase 1: Tests first (Constitution II, BLOCKS Phase 2)

- [x] T001 Create `tests/unit/test_139_optional_notification_channels.py` with helpers to build a config carrying a given notifications block. Verify it fails or is empty as appropriate.
- [x] T002 [P] Test FR-001/SC-001: a `slack` channel missing `webhook_url` does not prevent the daemon constructing its notification service; that channel is inactive. Verify it FAILS.
- [x] T003 [P] Test FR-002/SC-002: a routing entry naming a channel that does not exist does not prevent construction; the entry is ignored. Verify it FAILS.
- [x] T004 [P] Test FR-004/SC-003: `config validate` still reports **both** cases as errors. This is the half that keeps the relaxation honest — verify it PASSES already, and pin it.
- [x] T005 [P] Test FR-003: each skipped channel and ignored entry is reported once, naming what was skipped and why. Verify it FAILS.
- [x] T006 [P] Test FR-006/SC-004: exactly one startup posture line, naming active channels, skipped channels, and where stall signals will appear. Verify it FAILS.
- [x] T007 [P] Test FR-007/SC-005: no configured secret value appears in the posture line. Assert against the *values supplied*, not against a list of field names the implementation chose. Verify it FAILS.
- [x] T008 [P] Test FR-008/SC-006: a stall produces a warning-level log line with no channels and no dashboard reader. Verify it FAILS.
- [x] T009 [P] Test FR-009: a card that stays stuck does not repeat the log unboundedly, using the same dedup key as the notification path. Verify it FAILS.
- [x] T010 [P] Test FR-011/SC-008: zero channels loads and constructs cleanly — already true, locked against regression.
- [x] T011 [P] Test FR-010: a delivery failure remains non-fatal and does not block a card. Already true; pinned because this spec works next to it.

## Phase 2: Tolerant construction (US1 — the reported pain)

- [x] T012 In `services/notification.py`, build the active channel set from entries that validate, collecting the rest as (name, reason). Makes T002, T003, T005 pass.
- [x] T013 Ignore routing entries whose channels are absent **or** skipped, distinguishing the two in the reason. Makes T003 pass.
- [x] T014 (FR-004, FR-005, SC-007) Confirm nothing that already worked changed: run the existing notification and `config validate` tests **unmodified**. A fully-configured channel must behave exactly as before, and code that is not touched cannot regress — which is what makes this nearly free to satisfy and worth stating explicitly.

## Phase 3: The posture line (US2)

- [x] T015 Derive the posture from the constructed set — active names, skipped names with reasons, and the surfaces stall signals reach — and emit it once at startup in `__main__.py`. Makes T006 pass.
- [x] T016 Ensure no channel configuration value reaches the line; name channels only. Makes T007 pass.

## Phase 4: The stall log (US3)

- [x] T017 In `daemon.py`'s stall watchdog, log the stall at warning level unconditionally, before and independent of any dispatch, naming the card and duration. Makes T008 pass.
- [x] T018 Deduplicate using the existing `stuck:{card_id}:{phase}` key rather than a second policy. Makes T009 pass.

## Phase 5: Documentation

- [x] T019 Document that channels are optional, that a misconfigured channel degrades rather than stops coordinare, and that the log is the always-available stall surface. Note that `config validate` still reports the misconfiguration.
- [x] T020 [P] Test that the documentation states the always-available surface, so the claim cannot drift from the behaviour.

## Phase 6: Verification

- [x] T021 Full suite (`.venv/bin/pytest`, the way CI runs it — not a narrowed path) plus `make lint`.
- [x] T022 Re-read SC-001..SC-008 against what shipped; record anything not literally met, with the reason.
- [x] T023 Scope check: no subsystem other than notifications had its validation relaxed.
- [x] T024 Commit code and `specs/139-optional-notification-channels/` together, `Closes #187`.

## Dependencies

```
Phase 1 (T001-T011)   [BLOCKS everything; tests must fail first]
   ├─> Phase 2 (T012-T014)  US1, the reported pain
   ├─> Phase 3 (T015-T016)  US2, needs Phase 2's set
   └─> Phase 4 (T017-T018)  US3, independent of 2 and 3
          └─> Phase 5 (T019-T020) ─> Phase 6 (T021-T024)
```

## MVP

Phases 1–2 close the reported failure: coordinare starts with a half-configured channel. Phases 3–4
make the resulting state legible, which is what stops "I turned Slack off" from becoming "I cannot
tell if anything is wrong".
