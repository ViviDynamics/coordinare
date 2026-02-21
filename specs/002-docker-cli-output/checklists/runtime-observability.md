# Specification Quality Checklist: Runtime Observability & CLI/Docker Visibility

**Purpose**: Deep requirements quality audit — completeness, clarity, consistency, measurability, and coverage
**Created**: 2026-02-20
**Resolved**: 2026-02-20
**Feature**: `specs/002-docker-cli-output/spec.md`
**Depth**: Standard | **Audience**: Author / PR reviewer | **Focus**: Operational correctness, requirement gaps, measurability

---

## Requirement Completeness

- [x] CHK001 — Are requirements defined for all four runtime states (idle, active, blocked, recovery) named in FR-006? [Completeness, Spec §FR-006]
  > States are named in FR-006 and appear in contracts/shell-runtime-contract.md, but are not formally defined anywhere in the spec.
  > **Resolution**: FR-006 updated with formal definitions for all four states.

- [x] CHK002 — Does the spec define which failure types trigger a non-zero exit, consistent with the clarification "including recoverable ones"? [Completeness, Spec §FR-013]
  > FR-013 says "on runtime errors" without distinguishing recoverable from fatal. The clarification session resolves this.
  > **Resolution**: FR-013 updated to explicitly reference the recovery state and confirm non-zero exit follows regardless.

- [x] CHK003 — Are requirements defined for log rotation, buffering, or truncation behavior in long-running sessions? [Completeness, Gap]
  > Edge case "high output volume over long-running sessions" is listed in the spec but has no corresponding FR.
  > **Resolution**: Explicitly scoped out in Assumptions and edge cases section resolved with an out-of-scope note.

- [x] CHK004 — Are shutdown requirements defined for both graceful termination and failure-induced exit? [Completeness, Spec §FR-007]
  > FR-007 covers both cases explicitly.

- [x] CHK005 — Are requirements defined for the "blocked" and "recovery" runtime states beyond just naming them? [Completeness, Spec §FR-006]
  > US3 had no acceptance scenario for either the blocked or recovery state.
  > **Resolution**: FR-006 now formally defines both states; US3 now has acceptance scenarios 3 and 4 for blocked and recovery states.

- [x] CHK006 — Are all sensitive field keys formally enumerated in the spec? [Completeness, Spec §FR-014]
  > FR-014 lists: token, password, secret, api_key, webhook_url, authorization.

- [x] CHK007 — Are requirements defined for both human-readable and structured output modes? [Completeness, Spec §FR-011]

---

## Requirement Clarity

- [x] CHK008 — Is FR-010 ("configured sensitive fields") consistent with FR-014's fixed enumerated list? [Clarity, Conflict, Spec §FR-010, §FR-014]
  > FR-010 said "configured sensitive fields" implying runtime configurability, while FR-014 gave a hardcoded list.
  > **Resolution**: FR-010 rewritten to explicitly reference FR-014's list, removing the misleading "configured" framing.

- [x] CHK009 — Are valid log levels defined in the spec for use with FR-012? [Clarity, Gap, Spec §FR-012]
  > FR-012 referenced "log-level configuration" without enumerating valid levels.
  > **Resolution**: FR-012 updated to enumerate DEBUG, INFO, WARNING, ERROR and name the `--log-level` flag.

- [x] CHK010 — Is the `--structured-output` flag name defined in the spec, or only in contracts? [Clarity, Gap, Spec §FR-011]
  > The flag appeared only in shell-runtime-contract.md.
  > **Resolution**: FR-011 updated to name the `--structured-output` flag explicitly.

- [x] CHK011 — Is "actionable error output" (FR-005) sufficiently scoped by SC-003? [Clarity, Spec §FR-005, §SC-003]
  > SC-003 defines "enough context to identify the failing step" as a measurable outcome (≥90% of failures); acceptable for this phase.

- [x] CHK012 — Is the startup visibility window of 10 seconds (SC-001) consistent with the plan's stated performance goal? [Clarity, Spec §SC-001]
  > Plan states "Startup status visible within 10 seconds" — aligned.

- [x] CHK013 — Is case-insensitivity for sensitive field key matching stated in the spec? [Clarity, Spec §Assumptions]
  > "Sensitive-field matching is case-insensitive for key names" is documented in the Assumptions section.

---

## Requirement Consistency

- [x] CHK014 — Is the exit behavior for recoverable errors consistent between FR-013 and FR-006? [Consistency, Conflict, Spec §FR-013, §FR-006]
  > FR-006 implied the system transitions through a "recovery" state (suggesting continued operation), while FR-013 mandated non-zero exit on all errors.
  > **Resolution**: FR-006 now defines "recovery" as a transient output state before exit, not indefinite recovery. FR-013 explicitly cross-references this to remove the apparent conflict.

- [x] CHK015 — Are output semantics consistent between shell and Docker Compose execution modes per FR-008? [Consistency, Spec §FR-008]
  > Both runtime contracts show identical output taxonomy (startup/activity/heartbeat/shutdown). Consistent.

- [x] CHK016 — Does the heartbeat interval in FR-015 (≤30s) align with the state identification window in SC-002 (≤30s)? [Consistency, Spec §FR-015, §SC-002]
  > A 30-second heartbeat ceiling ensures at least one signal appears within the SC-002 window. Aligned.

---

## Acceptance Criteria Quality

- [x] CHK017 — Are measurement methods defined for SC-001, SC-002, SC-003, and SC-005? [Measurability, Gap, Spec §SC-001–SC-005]
  > Only SC-004 had a defined measurement method.
  > **Resolution**: Measurement methods added for SC-001, SC-002, SC-003, and SC-005.

- [x] CHK018 — Is SC-004's measurement method sufficiently concrete (sample size, pass threshold)? [Measurability, Spec §SC-004]
  > 10 first-time executions, ≥9/10 success. Concrete and verifiable.

- [x] CHK019 — Is the SC-004 trial checklist defined or linked in the spec? [Measurability, Gap, Spec §SC-004]
  > SC-004 referenced "a checklist-based run trial" but no checklist was linked.
  > **Resolution**: SC-004 measurement method now explicitly links to `specs/002-docker-cli-output/quickstart.md`.

- [x] CHK020 — Are all success criteria technology-agnostic and implementation-independent? [Acceptance Criteria, Spec §SC-001–SC-005]
  > All SCs are framed in terms of user-observable outcomes, not implementation specifics.

---

## Scenario Coverage

- [x] CHK021 — Is there an acceptance scenario for intentional graceful shutdown (SIGTERM/Ctrl+C)? [Coverage, Gap]
  > FR-007 defined the requirement, but no acceptance scenario validated it.
  > **Resolution**: US1 acceptance scenario 4 added covering SIGTERM/Ctrl+C graceful shutdown output.

- [x] CHK022 — Is there an acceptance scenario for the "blocked" runtime state? [Coverage, Gap, Spec §FR-006]
  > **Resolution**: US3 acceptance scenario 3 added for the blocked state.

- [x] CHK023 — Is there an acceptance scenario for the "recovery" runtime state? [Coverage, Gap, Spec §FR-006]
  > **Resolution**: US3 acceptance scenario 4 added for the recovery state.

- [x] CHK024 — Are acceptance scenarios defined for startup failure (invalid config)? [Coverage, Spec §US1 scenario 3]
  > US1 scenario 3 covers this explicitly.

- [x] CHK025 — Does US2 cover runtime failure visibility in Docker Compose? [Coverage, Spec §US2 scenario 3]
  > US2 scenario 3 covers failure reason and context in container logs.

---

## Edge Case Coverage

- [x] CHK026 — Is behavior defined when startup is attempted with missing or invalid required configuration values? [Edge Cases, Gap]
  > Edge case existed but wasn't resolved with an explicit requirement reference.
  > **Resolution**: Edge cases section updated to cross-reference FR-003 and FR-005 for startup validation output requirements.

- [x] CHK027 — Is behavior defined when the application is stopped mid-cycle (during active processing)? [Edge Cases, Spec §FR-007]
  > FR-007 addressed shutdown output but not in-flight work acknowledgment.
  > **Resolution**: FR-007 updated to require the shutdown message to indicate an interrupted (not completed) cycle when shutdown occurs during active processing.

- [x] CHK028 — Is redaction behavior defined for nested or compound field values containing sensitive keys? [Edge Cases, Spec §FR-014]
  > Assumptions state case-insensitivity, and FR-014 enumerates keys. Nested structure handling is unspecified but acceptable for this scope.

---

## Non-Functional Requirements

- [x] CHK029 — Is the target platform (OS/shell version) specified in the spec? [Non-Functional, Gap]
  > The plan stated "Linux shell environments and Linux Docker hosts" but the spec did not.
  > **Resolution**: Assumptions updated to specify Linux target platform, Bash ≥ 3.2.

- [x] CHK030 — Are performance targets defined for startup visibility and heartbeat cadence? [Non-Functional, Spec §SC-001, §SC-005]

- [x] CHK031 — Is a security requirement defined for sensitive data exposure via console/log output? [Non-Functional, Spec §FR-010, §FR-014]

---

## Dependencies & Assumptions

- [x] CHK032 — Are Docker and Docker Compose version requirements stated? [Dependencies, Gap]
  > The spec did not specify minimum Docker Engine or Compose plugin versions.
  > **Resolution**: Assumptions updated to specify Docker Engine ≥ 24.0 and Docker Compose plugin ≥ 2.20.

- [x] CHK033 — Is the assumption that no visual dashboard is required explicitly stated? [Assumptions, Spec §Assumptions]

- [x] CHK034 — Is the assumption that console output is the sole observability mechanism stated? [Assumptions, Spec §Assumptions]

---

## Summary

| Category | Total | Passed ✅ | Needs Attention ⚠️ |
|---|---|---|---|
| Requirement Completeness | 7 | 7 | 0 |
| Requirement Clarity | 6 | 6 | 0 |
| Requirement Consistency | 3 | 3 | 0 |
| Acceptance Criteria Quality | 4 | 4 | 0 |
| Scenario Coverage | 5 | 5 | 0 |
| Edge Case Coverage | 3 | 3 | 0 |
| Non-Functional Requirements | 3 | 3 | 0 |
| Dependencies & Assumptions | 3 | 3 | 0 |
| **Total** | **34** | **34** | **0** |
