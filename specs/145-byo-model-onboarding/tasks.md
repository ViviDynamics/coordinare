---
description: "Task list for BYO-model onboarding (Spec 145, issue #199)"
---

# Tasks: BYO-Model Onboarding

**Written after execution**, at the user's explicit direction to prioritise completion over
ceremony. Every item below was done and is marked accordingly; this records what happened rather
than planning what would. The reasoning lives in [spec.md](./spec.md), [plan.md](./plan.md) and
[research.md](./research.md), which were written before or during the work.

Recording it honestly matters more than backdating it: a checklist that pretends to have preceded
the work would misrepresent how the decisions were actually reached, several of which changed
because implementation contradicted the plan.

## Scope guardrails applied throughout

- `specs/` excluded from the internal-reference guard (historical record).
- No workflow changes; no lock-file changes.
- Presets validated against the real `ProjectConfiguration`, never a schema copy.
- Every preset keeps the dashboard on loopback, since it is unauthenticated by design (spec 144).

---

## Phase 1: The guard (US1)

- [x] T001 Write `tests/unit/test_145_byo_model_onboarding.py` with the internal-reference guard, scanning tracked files excluding `specs/`. Assemble the forbidden values at runtime (`"spark" + "/"`) so the module does not match its own guard, rather than excluding the file by path, which would leave the file most likely to reintroduce a value as the one never checked.
- [x] T002 Add `test_guard_fails_when_an_internal_reference_is_reintroduced` **before** the cleanup, and confirm the guard fails on all three values. This is the test spec 142's scrub needed and lacked.
- [x] T003 Add `test_guard_does_not_flag_legitimate_placeholders`, asserting the documentation placeholder and the spec-144 non-loopback fixtures survive, and that those values are still present in the tree so the assertion is not vacuous.

## Phase 2: Removing the references (US1)

- [x] T004 Replace `litellm.vividynamics.com` → `litellm.example`, `192.168.3.30` → `192.0.2.10` (RFC 5737), `spark/` → `local/` across tracked non-spec files. 57 files, 168 occurrences.
- [x] T005 Replace bare-word `spark`/`Spark` usages: `http://spark:11434` → `http://localhost:11434` (Ollama's real default, so the example is usable rather than merely anonymised), prose to "the model host", identifiers to `local-ollama`. 33 further files.
- [x] T006 Rename `scripts/pull_spark_models.sh` → `scripts/pull_models.sh` and genericise its contents; fix the reference in `_phase2_newmodels.sh`.
- [x] T007 Fix the two example configs broken by an inconsistent rename across the two passes (endpoint became `local-ollama-qwen3-6-35b`, its reference `local-qwen3-6-35b`) by validating that every mode reference resolves to a defined endpoint.
- [x] T008 Fix `test_security_ollama_routing_083.py`, which reads the operator's **live** `config.yaml`: the sweep had pinned it to a `local/` prefix, breaking it for any deployment naming its provider differently. Now asserts the behaviour rather than one deployment's naming.
- [x] T009 Correct spec 142's pre-public scrub, which recorded a false "0 files, Clean" for private addresses because its grep used `\b`, unsupported by `git grep -E`. Committed separately (`fe09d78`).

## Phase 3: Provider presets (US2)

- [x] T010 Write `config.example.ollama.yaml` with the minimum role set and `CHANGE ME` markers.
- [x] T011 Derive `config.example.openai-compatible.yaml` and `config.example.anthropic.yaml`.
- [x] T012 Fix three errors surfaced by validating against the real validator: `agent_transport: docker` is invalid; `github_token` is required; `kind: openai` rejects `base_url` (the generic OpenAI-compatible kind is `vllm`).
- [x] T013 Add preset tests: existence, real-validator validation, `CHANGE ME` markers, minimum role set, loopback dashboard.

## Phase 4: Preflight (US3)

- [x] T014 Write `src/coordinare/doctor.py`: endpoint reachability, model presence, dashboard bind. `CheckResult` refuses construction as a failure without a fix, enforcing the invariant in the type rather than by review.
- [x] T015 List what an endpoint *does* serve on a model mismatch, since that listing is nearly always the fix.
- [x] T016 Register the `doctor` subcommand and add `_load_project_config_for_doctor`, mirroring the daemon's own loading path (including multi-symphony wrapping) so preflight inspects the object the daemon would run with.
- [x] T017 Run `doctor` against a real Ollama endpoint rather than only mocks. It correctly reported the configured model absent and named the two present.
- [x] T018 Add preflight tests, including that a passing report states what it did **not** prove.

## Phase 5: Documentation (US2, US4)

- [x] T019 Write `docs/quickstart.md`: clone → first dispatched card, with the token-permission matrix linked and the two easy-to-miss permissions called out.
- [x] T020 Document the minimum viable role set (2 of 9) and that unconfigured roles are skipped, not failed.
- [x] T021 Fix the preset and quickstart invocations, which named `bin/coordinare validate` and `bin/coordinare doctor`. Neither existed. Now asserted against the real argument parser.

## Phase 6: Polish

- [x] T022 Fix the `smtp_sender` default, which leaked our domain into every self-hoster's outgoing mail.
- [x] T023 Fix the test helper that mutated `os.environ` and broke two unrelated config tests, which passed in isolation and failed in the suite.
- [x] T024 Full verification: 4927 unit, 490 contract/integration, 1353 performer. `make lint` clean.
- [x] T025 Commit, push, open PR #213, move the board.

## Outstanding, and not buildable here

- [ ] **Enable private vulnerability reporting** at the visibility flip. Public-repository-only, needs the GitHub UI. `SECURITY.md` already points at it.
- [ ] **Decide whether the `specs/` tree is published.** 660 of 1,999 files carry `spark/*`, 158 the internal gateway. Excluded from the guard deliberately as a historical record; publishing it is a separate decision nobody has made.
