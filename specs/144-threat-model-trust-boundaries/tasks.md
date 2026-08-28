---
description: "Task list for Threat model, trust boundaries, and cheap hardening (Spec 144, issue #198)"
---

# Tasks: Threat Model, Trust Boundaries, and Cheap Hardening

**Input**: Design documents from `/specs/144-threat-model-trust-boundaries/`
**Prerequisites**: spec.md, plan.md, research.md, data-model.md, contracts/localhost-guard.md, quickstart.md
**Tests**: Requested. Constitution II is non-negotiable, and for a security guard the tests are the deliverable as much as the code.
**Branch**: `144-threat-model-trust-boundaries`

## Format: `[ID] [P?] [Story] Description`

## Scope guardrails (apply to every task)

- **Do not add authentication.** Spec 143 (#197) owns that. This feature documents the
  unauthenticated posture and closes the CSRF and rebinding holes around it.
- **Do not change `docker.sock` mounting or performer sandboxing.** Documented here, not changed.
- **Do not change any existing workflow's** triggers, required status, or runtime (SC-009).
- **Do not restructure `SECURITY.md`.** Fill the reserved section in place (FR-008).
- **Do not change the effective `health_check_host` default.** Research D4 decided it stays
  `0.0.0.0` deliberately; changing it is a silent breaking change.
- No push, PR, or board move without explicit approval.

---

## Phase 1: Setup

- [x] T001 Create `tests/unit/test_144_threat_model_trust_boundaries.py` with `from __future__ import annotations`, a docstring naming spec 144 / issue #198 and the four user stories, imports (`ipaddress`, `pathlib`, `pytest`, `fastapi.testclient`), and `REPO_ROOT = Path(__file__).resolve().parents[2]`, following `tests/unit/test_142_license_and_legal_posture.py`.

---

## Phase 2: Foundational (blocks US2 and US3)

- [x] T002 Add `health_check_host: str = "0.0.0.0"` and `trusted_dashboard_hosts: list[str] = Field(default_factory=list)` to the config model in `src/coordinare/config.py`, next to `health_check_port` (:824) and `dashboard_host` (:826), mirroring the `trusted_bot_reviewers` idiom at :819. Comment `health_check_host`'s default with **why** it is `0.0.0.0` and not loopback (research D4: `README.md:119` documents the endpoint for load balancers; a containerised daemon on loopback is unreachable from the host), so a future reader does not "fix" it.

**Checkpoint**: Config additive and backward-compatible; existing config files load unchanged.

---

## Phase 3: User Story 2 - A hostile page cannot drive the dashboard (Priority: P1) 🎯 MVP

**Goal**: The localhost guard, exactly as specified in `contracts/localhost-guard.md`.

**Independent Test**: Foreign `Origin` on a mutating route is rejected; foreign `Host` on a read is rejected; absent `Origin` is allowed; the dashboard's own requests and the SSE stream still work.

**Why this is the MVP rather than US1**: it is the only exposure that needs nothing unusual from the operator. The documentation matters as much for launch, but a reader can be told about a hole; a hole cannot be closed by being described.

### Tests for User Story 2

- [x] T003 [P] [US2] In the test module, add permitted-set derivation tests against `localhost_guard`: `localhost`, `127.0.0.1`, `127.0.0.2` (in-range, must pass — the reason `ipaddress.is_loopback` is used rather than a spelling list), `::1`, the bracketed form `[::1]:8090`, a host with no port, a host with the wrong port, mixed case, and each `trusted_dashboard_hosts` entry. Assert a bare `::1` is NOT corrupted by port-stripping (research D2, parsing rule 2). Verify these FAIL first.
- [x] T004 [P] [US2] In the test module, add decision-table tests, one per numbered row of `contracts/localhost-guard.md`: absent `Host` rejected; foreign `Host` rejected; foreign `Host` rejected **on a GET** (the DNS-rebinding case, and the row most likely to be "simplified" away later); non-mutating with good `Host` allowed; absent `Origin` on a mutating request **allowed** (FR-010); foreign `Origin` on a mutating request rejected; good both allowed. Verify they FAIL first.
- [x] T005 [P] [US2] In the test module, add the fail-closed test (FR-012, SC-003): enumerate the constructed app's **actual routes** via `app.routes`, and for every route whose methods include a mutating verb, assert a foreign `Origin` is rejected. Assert against the live route table, never a hardcoded list of 24 paths — a fixed list goes stale and would defeat the property it claims to protect. Verify it FAILS first.
- [x] T006 [P] [US2] In the test module, assert rejections return 403 naming the failed header in the body, and that the rejected value is not echoed unescaped (FR-016, contract "Rejection response"). Verify it FAILS.
- [x] T007 [P] [US2] In the test module, assert the SSE `/events` route and the dashboard page load still succeed through the guard (FR-017).

### Implementation for User Story 2

- [x] T008 [US2] Create `src/coordinare/localhost_guard.py` implementing `PermittedOrigins` derivation and the middleware factory per `contracts/localhost-guard.md` and `data-model.md`. Use `ipaddress.is_loopback` rather than a spelling list. Handle IPv6 bracketing and the port rule per research D2's two parsing rules. Compute the permitted sets **once at construction**, never per request (Principle IV). Do not honour `X-Forwarded-Host` (research D3).
- [x] T009 [US2] Install the guard in `src/coordinare/dashboard.py` immediately before the existing `@app.middleware("http")` request logger at :3896, so rejected requests are refused before handler work while the logger still observes the 403.
- [x] T010 [US2] Update the **nine** existing test modules that construct a client against the dashboard app so it presents a permitted host (FR-018). Verified counts, 2026-08-27 — **13 constructions across 6 files**:

  | File | Constructions |
  |---|---|
  | `tests/unit/test_dashboard.py` | 7 (one with `raise_server_exceptions=False`) |
  | `tests/unit/test_cancel.py` | 2 |
  | `tests/unit/test_dashboard_config_api.py` | 1 |
  | `tests/unit/test_dashboard_config_integration.py` | 1 |
  | `tests/unit/test_dry_run.py` | 1 |
  | `tests/unit/test_symphony_coverage.py` | 1 |

  Pass `base_url="http://127.0.0.1:8090"`. **Do NOT add `testserver` to the permitted set** — the header is attacker-controlled, so that would be a permanent production hole.

  **Plus three more outside `tests/unit/*.py`**, found only when the full suite failed: `tests/unit/dashboard/test_force_poll.py` (1), `tests/contract/test_force_poll_contract.py` (1), `tests/integration/test_dashboard_quickstart.py` (1, multi-line form). **Nine files, sixteen constructions total.**

  Corrections from earlier drafts, all verified: `tests/unit/test_symphony_api_endpoints.py` does **not** construct a client and must not be touched (it matched a grep on API path strings, not on client construction). `tests/unit/dashboard/test_webhook.py` builds a **bare** `FastAPI()` rather than the dashboard app, so the guard is never installed there and its 6 constructions are correctly unaffected. `test_health_coverage.py` and `test_health_endpoints.py` target the **health** app, which is not guarded.

  Lesson worth carrying: this list was wrong twice because the enumeration globbed `tests/unit/*.py` and did not recurse. Enumerate with `grep -rn` across all of `tests/` before trusting a count.
- [x] T011 [US2] Run the full unit suite. Any failure in the six updated modules is expected to be a missed `TestClient` construction, not a guard bug — check that first. There are 13 to find.

**Checkpoint**: US2 independently shippable. The exploitable hole is closed.

---

## Phase 4: User Story 3 - An operator who exposes coordinare is told (Priority: P2)

- [x] T012 [P] [US3] In the test module, assert a non-loopback `dashboard_host` produces a startup warning naming the absence of authentication, and that a loopback bind produces **none** (FR-020, FR-021, SC-005). Verify they FAIL first.
- [x] T013 [P] [US3] In the test module, assert `health_check_host` defaults to `0.0.0.0` and is honoured when set. The assertion on the default is deliberate: it pins research D4's decision so a later "consistency fix" has to argue with a test rather than silently break a documented deployment.
- [x] T014 [US3] Emit the startup warning in `src/coordinare/__main__.py` when `config.dashboard_host` is non-loopback, using `ipaddress.is_loopback` via the shared helper from `localhost_guard` rather than a second implementation.
- [x] T015 [US3] Replace the hardcoded `"0.0.0.0"` at `src/coordinare/__main__.py:1034` and `:949` with `config.health_check_host`. Behaviour is unchanged at the default; the point is that it becomes configurable.

---

## Phase 5: User Story 1 - A self-hoster can see what they are trusting (Priority: P1)

**Note on ordering**: P1 by importance, sequenced after US2 and US3 so the document describes mitigations that already exist rather than ones it hopes for.

- [x] T016 [P] [US1] In the test module, assert `docs/security/threat-model.md` exists and each of the four trust boundaries has a non-empty residual-risk statement (data-model `TrustBoundary` validation rule, FR-003). A boundary with mitigations and no residual risk is either wrong or marketing.
- [x] T017 [P] [US1] In the test module, assert every test name cited by the threat model actually exists in the test suite (research D5). This is the ONLY automated check on the prose — do not add assertions on wording, which would fail on a rewrite and train people to weaken the test.
- [x] T018 [US1] Write `docs/security/threat-model.md` covering the four boundaries (FR-001), `docker.sock` as root-equivalent and an explicit trust decision (FR-002), prompt injection with mitigations **and** residual risk (FR-003), the unauthenticated dashboard as deliberate with spec 143 as its future (FR-004), what the health endpoints disclose including the `0.0.0.0` residual risk from research D4 (FR-005), and untrusted performer output with what the security gate does and does not catch (FR-006). Add the "How to verify these claims" section citing test names.
- [x] T019 [P] [US1] Write the token-permission matrix (FR-023, FR-024, FR-025), mapping each feature to its fine-grained permission and access level, distinguishing minimal-run from optional, and noting the GitHub App direction citing the spec-085 AppAuth foundations.
- [x] T020 [US1] Fill `SECURITY.md`'s reserved "Architecture and trust boundaries" section in place, replacing the placeholder comment and the interim operating assumptions with pointers into the threat model. Do not restructure the surrounding file (FR-008). The spec-142 test asserting `SECURITY.md` names spec 144 must still pass.
- [x] T021 [US1] Add the prominent README link (FR-007), framed as something to read before exposing coordinare to a network. Note the spec-142 wording guard covers `README.md` — do not introduce "open source", "OSI", or any warranty/support/response commitment language (FR-026 of spec 142 will fail the build).

---

## Phase 6: Polish

- [x] T022 Run the full unit suite; confirm no regression, especially in the five updated modules and in `tests/unit/test_142_license_and_legal_posture.py`, whose posture guards cover `README.md` and `SECURITY.md` which this feature edits.
- [x] T023 `make lint` and `make test` clean.
- [x] T024 Prove the guard has teeth per quickstart.md §3: add a temporary mutating route and confirm the fail-closed test rejects a foreign origin against it; add a temporary `trusted_dashboard_hosts` entry and confirm the corresponding `Host` rejection becomes a 200. Revert both. A guard that never fires is indistinguishable from one that is not installed.
- [x] T025 Re-read the four acceptance-criteria blocks against the tree. Record any criterion **not** met and why. Note SC-001 (a reader can name the boundaries and one unmitigated risk) has no mechanical test and must be judged by reading.
- [x] T026 Verify scope: `git diff --name-only main` touches no workflow file, and no authentication was added.
- [x] T027 Commit code and `specs/144-threat-model-trust-boundaries/` together, Conventional Commits, `Closes #198`. No push or PR without explicit approval.

---

## Dependencies

```
T001
 ├─→ T002 ─→ US2 (T003-T011) ── MVP, closes the exploitable hole
 │            └─→ US3 (T012-T015)
 └─────────────→ US1 (T016-T021), sequenced last so it documents what exists
Phase 6 depends on all.
```

**Real coupling**: T021 and T020 edit `README.md` and `SECURITY.md`, both covered by spec 142's posture guards. T022 must run those guards, not just this feature's tests.

## Implementation Strategy

**MVP is US2 alone** — it closes the exploitable hole. US1 is equally launch-blocking but is documentation, and documentation of a mitigation is better written after the mitigation exists.

**Suggested order**: T001 → T002 → US2 → US3 → US1 → Phase 6.

**One decision may need the approver**: research D4 keeps `health_check_host` defaulting to `0.0.0.0`, trading a tighter default for not silently breaking a documented load-balancer deployment. If the approver prefers the tighter default, T013's assertion and the threat model's residual-risk wording both change.

## Task Summary

| Phase | Tasks | Count |
|---|---|---|
| Setup | T001 | 1 |
| Foundational | T002 | 1 |
| US2 (P1, MVP) | T003-T011 | 9 |
| US3 (P2) | T012-T015 | 4 |
| US1 (P1) | T016-T021 | 6 |
| Polish | T022-T027 | 6 |
| **Total** | | **27** |
