# Tasks: Config Assistant

**Feature**: `155-config-assistant` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)
**Issue**: #202

Tests before implementation (Constitution II). Decisions settled in
[research.md](./research.md) — R1 above all: **one structured response per turn, never an
agentic tool loop.** A task that adds a tool-calling loop is wrong.

## Phase 1: Tests first — the safety properties

- [x] T001 (FR-002, SC-001) In `tests/unit/test_155_config_assistant.py`, assert over the AST
      that `services/config_assistant.py` neither imports nor calls anything that writes config.
      Structural, so it fails on the change that adds the capability rather than on observing it
      unused.
- [x] T002 [P] (FR-008, SC-002) A real-looking secret placed in config appears in **no** prompt
      the scripted backend received. Asserted on the actual prompt strings, not on the masking
      helper in isolation.
- [x] T003 [P] (FR-012, SC-006) Malformed model output — not JSON, JSON of the wrong shape,
      missing fields — yields a plain failure and never a proposal.
- [x] T004 [P] (FR-005) A proposal naming an unknown section, or an unknown field within a known
      section, is rejected before it reaches anything that could apply it.
- [x] T005 [P] (FR-009) A proposal setting a literal value for a secret-bearing field is
      rejected; setting an env-var placeholder for it is accepted.
- [x] T006 [P] (FR-013, FR-014, SC-005) With the feature disabled, the assistant endpoints are
      absent and the dashboard is otherwise unchanged.
- [x] T007 [P] (FR-011) One backend call per turn. Asserted on the call count, so an agentic loop
      reintroduced later fails here.
- [x] T008 [P] (FR-017) An oversized config produces a bounded prompt that states what was
      omitted rather than truncating silently.

## Phase 2: The assistant service

- [x] T009 (FR-001, FR-008) In `src/coordinare/services/config_assistant.py`, build the masked
      view from `config_descriptors`, reusing `serialize_value`/`SECRET_MASK` — no second
      masking implementation (research R4).
- [x] T010 (FR-011, FR-017) Build one bounded prompt per turn: section list always, detail for
      sections in play, omissions stated.
- [x] T011 (FR-010) Call the conducting backend from daemon state with
      `response_format="json"`, following `classify_issue_comment_ai`'s `None`-on-failure shape.
- [x] T012 (FR-003, FR-012) Parse into reply-or-proposal; anything unparseable is a failure, not
      a partial proposal.
- [x] T013 (FR-004, FR-005, FR-009) Validate a proposal against the real config models and the
      descriptors; reject unknown targets and literal secrets.

## Phase 3: The apply path

- [x] T014 (FR-006, FR-007, SC-003) Add an **optional** `expected_hash` to
      `PUT /api/config/global`, enforced with the existing `guard_concurrency` → 409. Optional so
      no existing caller or test changes (SC-008). See research R5 and #237.
- [x] T015 (FR-007) Test: a proposal built against a stale hash is refused; without a hash the
      endpoint behaves exactly as before.

## Phase 4: The dashboard

- [x] T016 (FR-018) Chat endpoints on the existing dashboard app, inside the same origin guard as
      the rest of the config surface, registered only when the feature is enabled.
- [x] T017 (FR-015) Session-scoped conversation state; nothing persisted.
- [x] T018 The panel itself, in the existing inline-JS style: messages, a proposal rendered as a
      diff, and an Apply button that sends the proposal with the hash it was built against.
- [x] T019 (FR-012) The panel shows a backend failure as a plain message and keeps working.

## Phase 5: First run

- [x] T020 (SC-007) With an unconfigured install, the assistant opens on the golden path rather
      than a blank prompt.
- [x] T021 (SC-007) Test: from an empty config, applying the proposals in order reaches a
      configuration that validates.

## Phase 6: Verification and review

- [x] T022 `.venv/bin/ruff check src tests`; `.venv/bin/mypy` on the new module.
- [x] T023 `.venv/bin/pytest tests/ -q`.
- [x] T024 (SC-008) `git diff --stat main...HEAD -- tests/` shows no existing test modified.
- [x] T025 (SC-005) Confirm by test that a disabled assistant leaves the dashboard unchanged.
- [x] T026 Acceptance re-read against SC-001..SC-008.
- [x] T027 Commit code and spec together, referencing #202.
- [x] T028 Adversarial `Workflow` review over the full branch diff. Lenses: a write path reached
      indirectly, secret leakage through any route the masking test does not cover, the parser
      accepting a malformed proposal, the concurrency guard being bypassable, and whether
      anything reintroduced a tool-calling loop.

## Dependencies

```
Phase 1 -> Phase 2 -> Phase 3 -> Phase 4 -> Phase 5 -> Phase 6
```

US1 (propose, cannot apply) and US3 (secrets) are Phase 1-2. US2 (apply) is Phase 3-4. US4
(first run) is Phase 5 and depends on the rest being right.

## Implementation strategy

MVP is Phases 1-3: an assistant that proposes, validated, unable to write, with a guarded apply.
Phase 4 makes it usable; Phase 5 makes it the onboarding path the issue is really asking for.

## Phase 7: Review remediation (adversarial `Workflow`, 11 confirmed / 3 refuted)

- [x] T029 **Apply could write with no guard at all.** The baseline hash was fetched
      fire-and-forget, so a proposal could be rendered before it arrived, or after the fetch
      failed — and Apply sent no `expected_hash`, silently unguarded, exactly when the
      dashboard was already struggling to reach the server. The turn now awaits its
      baseline, and Apply refuses without one. "Applying always goes through the guard" has
      to be true or it is not a claim.
- [x] T030 **A proposal's values were never checked.** Field names existing is not the same
      as the values working: `log_level: "verbose"` is a real field and an invalid value.
      The operator read a sensible diff, clicked Apply, and got a 400 — making the button
      where errors surface, which is what FR-004 exists to prevent. Now validated against
      the real config model, so ranges, patterns and enums are all covered and cannot drift
      from the schema.
- [x] T031 **A literal secret could ride alongside a placeholder.** `is_env_placeholder`
      asks whether a value *contains* a reference, which is right for display and wrong
      here: `"${TOKEN} ghp_real"` passed. The proposal check now requires the value to be
      only a reference.
- [x] T032 **Routes and page could disagree about the flag.** Routes are registered once at
      startup; the page checked per request. A config reload flipping the flag would serve a
      panel whose endpoints did not exist. One captured value now feeds both, at the cost of
      a restart to change it.
- [x] T033 **The secrets claim was broader than the guarantee.** Config secrets are masked;
      text the operator types is not, and cannot usefully be. The wording risked an operator
      pasting a token to ask about it. Narrowed in the module, the spec and US3, and stated
      in the panel where someone is about to type.
- [x] T028 Adversarial `Workflow` review over the full branch diff.

Two findings were confirmations rather than defects (severity `none`): that the five core
security properties hold, and that `esc()` is correctly applied to every `innerHTML`
insertion. Three were refuted.
