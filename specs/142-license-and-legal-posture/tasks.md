---
description: "Task list for License and legal posture for public release (Spec 142, issue #196)"
---

# Tasks: License and Legal Posture for Public Release

**Input**: Design documents from `/specs/142-license-and-legal-posture/`
**Prerequisites**: spec.md, plan.md, research.md, data-model.md, contracts/accepted-licenses.md, quickstart.md
**Tests**: Requested. Constitution II is non-negotiable and this feature's verifiable surface is
exactly what stops the posture from rotting. Test tasks precede implementation per story.
**Branch**: `142-license-and-legal-posture`

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: US1 / US2 / US3 / US4 (maps to spec.md user stories)

## Path Conventions

Repository-root documents, `.github/`, three `pyproject.toml` files, one test module at
`tests/unit/`, and recorded evidence in `specs/142-license-and-legal-posture/`.

## Scope guardrails (apply to every task)

- **Do not touch `src/coordinare/`.** This feature adds no runtime code.
- **Do not modify any existing workflow** (`main-branch-build.yml`, `pr-ci.yml`,
  `sync-version-to-prs.yml`) triggers, required status, or runtime (SC-009).
- **Do not modify `uv.lock` or `agent/performer/uv.lock`.** The audit reads them only.
- Never say "open source" or "OSI" of coordinare in any file this feature writes (FR-006).
- No push, PR, board move, or remote change without explicit approval.
- Reminder from spec 132's guardrails: keep `agent/performer/uv.lock` entirely out of this
  feature's diff.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Create the single test module all four stories add to, and the shared helpers the
two enforcement checks need.

- [x] T001 Create the test module scaffold `tests/unit/test_142_license_and_legal_posture.py` with `from __future__ import annotations`, a module docstring referencing spec 142 / issue #196 and naming the four user stories, imports (`pathlib`, `tomllib`, `importlib.metadata`, `re`, `pytest`), and a `REPO_ROOT = Path(__file__).resolve().parents[2]` constant, mirroring the structure of `tests/unit/test_132_onboarding_config_hardening.py`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The accepted-license data and the dependency-closure derivation are shared by US4's
check and US1's first-party assertions, so they land before either story.

**Note**: US1, US2 and US3 depend only on T001. Only US4 needs T002 and T003.

- [x] T002 In the test module, encode the accepted license set from `contracts/accepted-licenses.md` as three module-level frozensets (`TIER1_PERMITTED`, `TIER2_EXCEPTION`, `TIER3_REJECTED`) plus a `LICENSE_ALIASES` mapping that normalizes the historical spellings observed in real metadata (`BSD License`, `Apache Software License`, `MIT license`, `3-Clause BSD License`, `Modified BSD License`, `Apache License 2.0`, `Apache License, Version 2.0`, `Mozilla Public License 2.0 (MPL 2.0)`) onto their SPDX identifiers, and a `FIRST_PARTY = {"coordinare", "performer", "coordinare-service-inference"}` constant.
- [x] T003 In the test module, implement `_distributed_closure(lock_path: Path, root_names: list[str]) -> set[str]` which parses a `uv.lock` with `tomllib` and returns the transitive closure over each root package's `dependencies` list, excluding `optional-dependencies` and dev groups; and `_resolve_license(dist_name: str) -> tuple[str, str]` which reads installed metadata via `importlib.metadata` and returns `(license_id, source_field)` using the precedence `License-Expression` → short free-text `License` (reject values over 40 characters as pasted license text, not identifiers) → `License ::` classifier → `("UNKNOWN", "none")`.

**Checkpoint**: Foundation ready. The closure walk must return 63 packages for `coordinare` from the root lock (verified 2026-08-27) — assert that it returns a plausible non-empty set rather than hardcoding 63, which would break on the next legitimate dependency change.

---

## Phase 3: User Story 1 - A stranger can tell what they are allowed to do (Priority: P1) 🎯 MVP

**Goal**: `LICENSE`, `NOTICE`, the README licensing section, and license declarations in all
three packaging files, so the repository is legally publishable and a reader can determine their
rights in under two minutes.

**Independent Test**: On a clean clone, `LICENSE` and `NOTICE` exist at the root, the README's
licensing section states the permissions, the prohibition (including that it applies whether or
not money changes hands) and the commercial route, no posture file claims coordinare is open
source, and all three `pyproject.toml` files declare the ELv2 reference with no OSI classifier.

### Tests for User Story 1

- [x] T004 [P] [US1] In the test module, add tests asserting `LICENSE` exists at the repository root and holds the ELv2 text **unmodified**: assert it contains all nine canonical section headings (`Acceptance`, `Copyright License`, `Limitations`, `Patents`, `Notices`, `No Other Rights`, `Termination`, `No Liability`, `Definitions`), the distinctive hosted-service limitation clause, the warranty disclaimer, and the generic definition `The **licensor** is the entity offering these terms`. Assert NO placeholder token (`[`-bracketed stub, `{{`) and NO injected designation line inside the license body — ELv2 has no designation fields, so a `Licensor:` line in the body would mean the text was edited (FR-001, corrected 2026-08-27). Verify it FAILS before implementation.
- [x] T005 [P] [US1] In the test module, add a test asserting `NOTICE` exists, contains a copyright statement naming `Vivi Dynamics LLC` and the year 2026, identifies `coordinare` as the licensed work (this file performs the identification ELv2's generic body does not — FR-001a), and that no per-file copyright header was introduced (assert a sample of `src/coordinare/*.py` files carry no `Copyright` line, guarding FR-002's "no per-file headers"). Verify it FAILS.
- [x] T006 [P] [US1] In the test module, implement the file-scoped wording guard per research R2: a `POSTURE_FILES` constant listing `README.md`, `LICENSE`, `NOTICE`, `CONTRIBUTING.md`, `SECURITY.md` and the user-visible files under `.github/`; a test asserting none of them contains `open source`, `open-source`, or `OSI` (case-insensitive) except for entries in an explicit `WORDING_ALLOWLIST: list[tuple[str, str]]` that starts empty; a test asserting the README contains `source-available`; and the FR-026 guard asserting no posture file states or implies a warranty, a support obligation, or a response commitment (match on `warrant`, `guarantee`, `SLA`, `we will respond`, `supported version`, and the timeframe patterns from T026), noting that ELv2's own warranty DISCLAIMER inside `LICENSE` must be allowlisted rather than flagged — it disclaims a warranty, which is the opposite of offering one. Verify the guard FAILS on a temporary README edit claiming coordinare is open source, and that it does NOT flag `docs/opencode-sdk.md` or `src/coordinare/services/scoring.py` (assert those paths are outside `POSTURE_FILES`).
- [x] T007 [P] [US1] In the test module, add tests asserting the README licensing section states each of: the permissions (run, copy, modify, self-host, commercial use), the prohibition covering provision of coordinare's functionality as a hosted or managed service, that the prohibition applies regardless of payment (FR-005a), that future versions may carry different terms, the feedback-not-pull-requests model, and a commercial-licensing route (FR-024); and that the README contains exactly one `## License` heading, so the existing stub was replaced rather than duplicated. Verify it FAILS.
- [x] T008 [P] [US1] In the test module, add a test asserting each of `pyproject.toml`, `agent/performer/pyproject.toml` and `packages/service_inference/pyproject.toml` declares `license = "LicenseRef-Elastic-License-2.0"` and that none carries any `License ::` classifier (research R7 — a classifier asserts OSI approval, which is the exact claim FR-006 forbids). Verify it FAILS.

### Implementation for User Story 1

- [x] T009 [US1] Write the canonical Elastic License 2.0 text to `LICENSE` at the repository root **unmodified** — no designation edits, because ELv2 has none (FR-001, research R1 as corrected). Source: Elastic's canonical plaintext at `https://raw.githubusercontent.com/elastic/elasticsearch/main/licenses/ELASTIC-LICENSE-2.0.txt` (Elastic's own repository, i.e. the license steward, retrieved 2026-08-27, 93 lines / 3860 bytes, retaining the `URL: https://www.elastic.co/licensing/elastic-license` pointer it carries). Optionally prepend a short attribution header (`coordinare is licensed by Vivi Dynamics LLC under the Elastic License 2.0`) separated from the license body by a horizontal rule, so the identification is visible without altering the licensed text.
- [x] T010 [P] [US1] Write `NOTICE` at the repository root with a single copyright statement (`Copyright 2026 Vivi Dynamics LLC`), an identification of coordinare as the licensed work, and one sentence pointing at `LICENSE` for terms. No per-file headers anywhere.
- [x] T011 [US1] Replace the existing `## License` stub at `README.md:241-243` (currently a one-line pointer to a file that does not exist) with the full licensing and contribution section: the permissions paragraph, the prohibition paragraph including the payment-irrelevance point and without adjudicating boundary cases (FR-005a, FR-005b), the commercial-licensing pointer adjacent to the prohibition (FR-024), the future-terms sentence, and the feedback-not-pull-requests sentence. Use "source-available" and never "open source". The commercial enquiry route is `https://vividynamics.com/contact` (confirmed by the approver 2026-08-27; verified to return HTTP 200 the same day, so the published link is known-good rather than assumed).
- [x] T012 [P] [US1] Add `license = "LicenseRef-Elastic-License-2.0"` and `license-files = ["LICENSE", "NOTICE"]` to `pyproject.toml`, adding no `License ::` classifier. Verify the build backend accepts the PEP 639 fields; if it rejects `license-files`, keep `license` and record the omission in the audit rather than falling back to a deprecated table form.
- [x] T013 [P] [US1] Add the same PEP 639 license declaration to `agent/performer/pyproject.toml`, no classifier.
- [x] T014 [P] [US1] Add the same PEP 639 license declaration to `packages/service_inference/pyproject.toml`, no classifier. This is also FR-004: the in-tree first-party package is covered by the root license and must carry no separate or conflicting license file — assert no `LICENSE` file exists under `packages/service_inference/`.
- [x] T015 [US1] Run `.venv/bin/pytest tests/unit/test_142_license_and_legal_posture.py -v` and confirm every US1 test now passes. Run `make lint` and `make fmt`.

**Checkpoint**: US1 complete and independently shippable. The repository is legally publishable at this point even if nothing else lands.

---

## Phase 4: User Story 2 - A would-be contributor learns the policy before wasting effort (Priority: P2)

**Goal**: `CONTRIBUTING.md`, the pull request template, three issue templates, and the automation
that comments, labels and closes external pull requests while staying silent on internal ones and
leaving security-related submissions open.

**Independent Test**: Every GitHub touchpoint (contributing link, pull request template, issue
chooser) states the closed-pull-request policy; the workflow is shaped correctly by inspection
(`pull_request_target`, no checkout, minimal permissions, internal and bot paths exit early,
security hits do not close); a live external pull request receives the policy without maintainer
action.

### Tests for User Story 2

- [x] T016 [P] [US2] In the test module, add tests asserting `CONTRIBUTING.md` exists and contains: a welcome for issues, bug reports, feature requests and feedback; guidance on what a useful report contains; a statement that public pull requests are not accepted; the words identifying it as a supply-chain security measure; and a statement that such pull requests will be closed unmerged. Assert it contains no apology wording (no "sorry", no "unfortunately") per FR-008's tone requirement, and that it does not read as a build-and-test how-to (assert absence of `pytest`, `make test`, `uv sync`). Verify it FAILS.
- [x] T017 [P] [US2] In the test module, add tests asserting `.github/pull_request_template.md` exists and states the closed-pull-request policy, and that `.github/ISSUE_TEMPLATE/` contains `config.yml`, `bug_report.yml`, `feature_request.yml` and `feedback.yml`, each parsing as valid YAML with a non-empty `name` and `description`. Verify it FAILS.
- [x] T018 [US2] In the test module, add tests over `.github/workflows/external-contributions.yml` parsed as YAML asserting: the trigger is `pull_request_target` and not `pull_request`; no step uses `actions/checkout` anywhere in the file (research R4 — this is the single line that separates a safe `pull_request_target` job from a privilege-escalation hole); the `permissions` block grants `pull-requests: write` and no other write scope; the job conditions exclude `OWNER`, `MEMBER`, `COLLABORATOR` and `user.type == 'Bot'`; and the security carve-out path applies a comment and a label but issues no close call (FR-025). Verify it FAILS.
- [x] T019 [P] [US2] In the test module, add a test asserting the three existing workflows are unmodified in the ways SC-009 protects: `main-branch-build.yml`, `pr-ci.yml` and `sync-version-to-prs.yml` each still declare their original `on:` triggers and `runs-on` targets. This is a regression guard, not a new requirement — it exists so a later edit to this feature cannot silently disturb CI.

### Implementation for User Story 2

- [x] T020 [US2] Write `CONTRIBUTING.md` at the repository root as a policy document: issues and feedback are welcome and read and that they inform the roadmap, with NO promise of triage, response, or implementation (FR-008, decided 2026-08-27), what a useful report contains, and public pull requests are not accepted as a deliberate supply-chain security measure and will be closed unmerged. Friendly and unapologetic, no apology wording, not a how-to. Do not promise a forwarding or response-time mechanism that does not exist (spec 152 / #210 owns forwarding).
- [x] T021 [P] [US2] Write `.github/pull_request_template.md` stating the closed-pull-request policy up front, so an external author sees it before the automation runs, while remaining usable for internal pull requests (which are the overwhelming majority of its readers).
- [x] T022 [P] [US2] Write `.github/ISSUE_TEMPLATE/config.yml`, `bug_report.yml`, `feature_request.yml` and `feedback.yml` as GitHub issue forms. Structure the fields so a future forwarder (spec 152 / #210) can extract them deterministically without parsing prose.
- [x] T023 [US2] Write `.github/workflows/external-contributions.yml`: `pull_request_target` on `opened` and `reopened`; a GitHub-hosted runner (confirm availability with the approver, and if only self-hosted runners exist, note the API-only constraint in a comment in the file); `permissions: pull-requests: write` and nothing else; **no `actions/checkout` step**; classification from `github.event.pull_request.author_association` and `user.type` per research R4; on external, post the policy comment linking `CONTRIBUTING.md`, apply the `external-contribution` label, then close; on external-and-security-keyword-match, comment and label but do not close (FR-025). Add a header comment explaining why `pull_request_target` is required and why checkout must never be added.
- [x] T024 [US2] Create the `external-contribution` label in the repository so the workflow's label call cannot fail on a missing label. Requires approval before touching remote state.
- [x] T025 [US2] Run the US2 tests and confirm they pass. Run `make lint`. Verify the new workflow's YAML parses. Note `gh workflow list` cannot show it until the branch is pushed, so registration is confirmed after push rather than here.

- [ ] T025a [US2] Record the SC-003 live verification as an explicit post-merge step in the pull request description: after merge, have an account outside the organization open a throwaway pull request, then confirm the comment appears, the `external-contribution` label is applied, the pull request closes, and that the next internal pull request produces no response. This CANNOT be verified before merge (it needs a real fork-originated event from an outside account), so it must be carried forward in writing rather than left implied by T018's shape-only inspection.

**Checkpoint**: US2 complete apart from T025a, which is deliberately open until after merge.

---

## Phase 5: User Story 3 - A security researcher can report privately (Priority: P2)

**Goal**: `SECURITY.md` with an explicit no-support-commitment statement, the private reporting route, an explicit
no-timeframe-promised statement, and a marked seam for spec 144's threat-model pointers.

**Independent Test**: A reader finds `SECURITY.md` from the repository root and can identify which
versions receive fixes and exactly how to report privately, and can see that no response
timeframe is promised.

### Tests for User Story 3

- [x] T026 [P] [US3] In the test module, add tests asserting `SECURITY.md` exists and contains: a statement that no release carries a support commitment; a pointer to GitHub private vulnerability reporting; a statement that reports are read and answered as capacity allows; **no response-time commitment of any kind** — assert `SECURITY.md` matches no timeframe pattern (`\d+\s*(business\s*)?(hour|day|week|month)s?`, `within \d+`, `SLA`), so a later well-meaning edit cannot quietly reintroduce a promise nobody is owed; an instruction for reporters who cannot use advisories to request a private channel in a public issue **without** vulnerability detail (FR-015); no email address at all (assert no `@`-bearing address, since the resolved decision publishes none); and a clearly marked section reserved for spec 144's architecture and trust-boundary pointers (FR-017). Verify it FAILS.

### Implementation for User Story 3

- [x] T027 [US3] Write `SECURITY.md` at the repository root: an explicit no-support-commitment statement (no release, including the latest, carries a support obligation; fixes land on `main` when they land) rather than a supported-versions table, since "fixes land on version X" is itself a commitment (FR-014, decided 2026-08-27); GitHub private vulnerability reporting as the only route, with the no-detail-in-public-issues fallback instruction; a statement that reports are read and answered as capacity allows, committing to NO timeframe for acknowledgment, assessment or fix (decided 2026-08-27 — coordinare is given away free with no support contract, so publishing a figure would manufacture an expectation nobody is owed); and an explicitly marked placeholder section for spec 144 (#198) to fill with threat-model and trust-boundary pointers. Add a short note that the reporting route becomes live when the repository becomes public, so the document is not read as advertising a channel that is currently disabled.
- [x] T028 [US3] Run the US3 tests and confirm they pass. Run `make lint`.

**Checkpoint**: US3 complete. Note that FR-016 (actually enabling private vulnerability reporting) is deliberately **not** a task here — see T032.

---

## Phase 6: User Story 4 - The maintainer can prove the repository is safe to publish (Priority: P3)

**Goal**: The dated dependency license audit, the automated allowlist check that keeps it honest,
and the executed pre-public scrub checklist.

**Independent Test**: A reviewer reads the audit and sees every distributed dependency with a
license and a verdict, each Tier 2 exception carrying a rationale naming how it is consumed; and
walks the scrub checklist where every item names a concrete check and records an outcome.

### Tests for User Story 4

- [x] T029 [US4] In the test module, add the FR-020 check: for both distributed projects (root `uv.lock` from `coordinare`, and `agent/performer/uv.lock` from `performer`), derive the runtime closure via `_distributed_closure` (T003), resolve each package's license via `_resolve_license` (T003), and assert every package is in `TIER1_PERMITTED` or `TIER2_EXCEPTION` after alias normalization, with `FIRST_PARTY` packages exempt from the tiers and instead asserted to declare the ELv2 reference. On failure the assertion message must name the offending package and its license (FR-020, SC-007). Assert a package in the closure with no installed metadata is a hard failure rather than a skip (data-model validation rule). Verify the check FAILS when a Tier 3 license is injected into the tier sets as a temporary fixture.
- [x] T030 [P] [US4] In the test module, add a test asserting `specs/142-license-and-legal-posture/dependency-license-audit.md` exists, carries a date, lists every package the T029 closure produces, and that every Tier 2 entry in it carries a non-empty rationale and a `consumed_as` value (data-model `LicenseVerdict` validation rule: an exception without a rationale fails). Verify it FAILS.

### Implementation for User Story 4

- [x] T031 [US4] Generate and write `specs/142-license-and-legal-posture/dependency-license-audit.md` using the same derivation the T029 check uses, so the document and the check cannot disagree. Include: the date; both distributed projects; every package with name, pinned version, license identifier, and which metadata field supplied it; a verdict per package; and for each of the six known Tier 2 packages a written rationale recording that it is unmodified and how it is consumed. Record `semgrep` explicitly per research R6 — LGPL-2.1-or-later, invoked as a subprocess (aggregation, not a combined work), distributed as content of the performer image via `agent/performer/Dockerfile.full`. Note in the audit that `psycopg`/`psycopg-pool` reach us through the declared runtime dependency `langgraph-checkpoint-postgres`.
- [x] T032 [US4] Write `specs/142-license-and-legal-posture/pre-public-scrub.md` per the data-model `ScrubItem` shape, every item naming a concrete command or UI step with a `scope`, `owner`, `timing` and a place to record its outcome. Must include, marked `at_flip`: enabling GitHub private vulnerability reporting, which is impossible today because the repository is private and the feature is public-repository-only, and which needs the GitHub web UI because neither available token can write repository administration settings (research R5, FR-016). Attribute removal of internal addresses, hostnames and `spark/*` model identifiers to spec 145 / #199 rather than duplicating it (FR-022). Recommend, without requiring, enabling secret scanning and push protection at flip time (currently disabled) while noting hardening posture belongs to spec 144 / #198.
- [x] T033 [US4] Execute the scrub checklist's `now` items and record each outcome in the file (FR-023). At minimum: grep the working tree and git history for `.env` contents, tokens, private keys and credential patterns; confirm `.gh_token` and `.env` are gitignored and were never committed; and enumerate what the `at_flip` items leave outstanding.
- [x] T034 [US4] Run the US4 tests and confirm they pass. Run `make lint`.

**Checkpoint**: All four stories complete.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [x] T035 Run the full unit suite (`.venv/bin/pytest tests/unit/ -q`) and confirm no existing test regressed. (Checked 2026-08-27: no existing test module asserts over `README.md`, `LICENSE` or `NOTICE`, so no specific collision is expected — this is a general regression run, not a targeted one.)
- [x] T036 Run `make lint` and `make test` clean. Confirm `make test-all` if contract tests are in scope for the final gate.
- [x] T037 Confirm the wording guard has teeth per quickstart.md step 1: temporarily claim coordinare is open source in the README, observe the failure names the file, revert. A check that never fails is indistinguishable from one that is not running.
- [x] T038 Re-read the four spec acceptance-criteria blocks against the working tree and confirm each is met. Record any acceptance criterion that is **not** met and why, rather than leaving it implied. Three criteria have no mechanical test and MUST be judged by reading, not assumed from a green suite: FR-005b (the README does not adjudicate the prohibition's boundary cases), SC-001 (a stranger can determine their rights in under two minutes), and the tone requirement in FR-008 (friendly, unapologetic).
- [x] T039 Verify no file under `src/coordinare/` was modified (`git diff --name-only main -- src/coordinare/` returns empty) and that `uv.lock` and `agent/performer/uv.lock` are unmodified.
- [x] T040 Commit code and `specs/142-license-and-legal-posture/` together with a Conventional Commits message referencing `Closes #196`. Do not push, open a PR, or move the board without explicit approval.

---

## Dependencies

```
T001 (setup)
 ├─→ US1 (T004-T015)  ── independently shippable, MVP
 ├─→ US2 (T016-T025)
 ├─→ US3 (T026-T028)
 └─→ T002, T003 (foundational, US4 only)
      └─→ US4 (T029-T034)

US1, US2, US3 are mutually independent and may proceed in any order or in parallel.
US4 depends on T002/T003 but not on US1-US3.
Phase 7 depends on all.
```

**One real cross-story coupling**: T008/T014 (US1) assert the first-party packages declare ELv2,
and T029 (US4) exempts those same packages from the tier check on that basis. If US1 is skipped,
T029's first-party assertion fails. Land US1 before US4, or accept that ordering.

## Parallel Execution Examples

**Within US1**, T004, T005, T006, T007 and T008 touch only the test module's separate test
functions and may be written together; T010, T012, T013 and T014 touch four different files and
may be applied together. T009 and T011 are sequential against `LICENSE` and `README.md`
respectively.

**Across stories**, US1, US2 and US3 write disjoint files and have no shared state. Three agents
could take one story each after T001.

**Not parallelizable**: T003 depends on T002's constants. T029 depends on both. T031 must run
after T029 exists, since the audit is generated by the same derivation.

## Implementation Strategy

**MVP is US1 alone.** It makes the repository legally publishable, which is the launch gate's
actual requirement. US2 and US3 are cheap and should follow in the same pull request, but if
anything forces a split, US1 stands on its own.

**Suggested order**: T001 → US1 → US3 (smallest) → US2 → T002/T003 → US4 → Phase 7.

**One task still needs the approver**: T023/T024 (GitHub-hosted runner availability, and
creating the label touches remote state). T011's enquiry destination was confirmed 2026-08-27 as
`https://vividynamics.com/contact`.

## Task Summary

| Phase | Tasks | Count |
|---|---|---|
| Setup | T001 | 1 |
| Foundational | T002-T003 | 2 |
| US1 (P1, MVP) | T004-T015 | 12 |
| US2 (P2) | T016-T025a | 11 |
| US3 (P2) | T026-T028 | 3 |
| US4 (P3) | T029-T034 | 6 |
| Polish | T035-T040 | 6 |
| **Total** | | **41** |
