# Feature Specification: A settle-aware egress probe

**Feature Branch**: `154-egress-probe-settle`
**Created**: 2026-08-30
**Status**: Draft
**Issue**: #233 (follow-up to spec 146 / #225)

## Context

`coordinare doctor` runs a live probe that tells an operator whether their cluster actually
enforces NetworkPolicy, by proving a target is reachable with no policy and then unreachable
with a deny-all in place. That distinction matters: coordinare runs AI-generated code in
performer Pods, and a cluster that accepts a NetworkPolicy without enforcing it gives an
operator containment that looks real and is not.

The probe applies the policy and immediately starts the Pod that is supposed to be blocked.
Applying a NetworkPolicy and having the CNI program it are not the same event. In the window
between them, traffic flows — and the probe reads that as "this cluster does not enforce
NetworkPolicy".

This was observed once, in a full-suite run: the probe reported not-enforced, then passed on a
re-run and on `main`. A flake, but the flake is the probe being wrong, not the test being
fragile.

**Which way the error runs.** The probe already refuses to claim enforcement it has not proven:
without a reachable control it reports inconclusive rather than "enforced", because telling an
operator their performers are contained when they are not is the failure that must never be
guessed. This race runs the other way — claiming *no* protection where there is some. That is
the safer direction, and still wrong: an operator who is told their cluster ignores
NetworkPolicy will go and build something else, or trust nothing.

## Clarifications

- Q: Wait a fixed period after applying the policy before measuring? → A: Not on its own. A
  fixed sleep is a guess about someone else's cluster, and it is either too short to be safe or
  too long to be pleasant.
- Q: Poll until the policy is observably in effect? → A: That is the same measurement as the
  probe itself, so it cannot be a precondition for it. What it can be is a *repeat*.
- Q: Then what? → A: Never conclude "not enforced" from a single reach. A reach under policy is
  re-measured; only a reach that survives re-measurement is a verdict.
- Q: Should a cluster that genuinely does not enforce still be reported as such? → A: Yes, and
  clearly. Turning that into "inconclusive" would trade a rare wrong answer for a common
  useless one.
- Q: Does the live test need changing too? → A: Yes. It compares the probe against a hand-run
  deny-all that races identically, so the comparison can disagree even when the probe is right.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The probe does not cry wolf (Priority: P1)

An operator runs `coordinare doctor` against a cluster that does enforce NetworkPolicy. The probe
happens to measure during the window before the CNI has programmed the policy. It does not tell
the operator their cluster is unprotected.

**Why this priority**: this is the defect. Everything else here supports it.

**Independent Test**: simulate a cluster where the first measurement under policy reaches and
the next does not. The verdict is "enforced".

**Acceptance Scenarios**:

1. **Given** a cluster whose policy takes effect late, **When** the probe measures, **Then** it
   reports enforcement rather than its absence.
2. **Given** a cluster that genuinely does not enforce, **When** the probe measures, **Then** it
   still says so plainly — the fix must not launder a true negative into "inconclusive".
3. **Given** a re-measurement that cannot be taken at all, **When** the probe concludes,
   **Then** it reports inconclusive rather than either verdict.

---

### User Story 2 - The operator can see which measurement they got (Priority: P2)

An operator reading `doctor` output can tell "blocked immediately" from "blocked only after the
policy settled" from "reached every time".

**Why this priority**: independent of the fix, and it is what makes the result trustworthy
rather than merely different. A probe that silently retried would be a probe nobody could audit.

**Independent Test**: read the verdict detail for each of the three outcomes; each names what
was actually observed.

**Acceptance Scenarios**:

1. **Given** a policy that took effect late, **When** the operator reads the detail, **Then** it
   says the first attempt got through and a later one did not.
2. **Given** any verdict, **When** the operator reads it, **Then** nothing claims an observation
   the probe did not make.

---

### Edge Cases

- **The re-measurement itself never runs.** A Pod that never started produces no traffic, and
  reading silence as containment is the failure this probe exists to avoid.
- **A cluster that enforces intermittently.** Genuinely alarming, and it must not be smoothed
  into "enforced".
- **The probe taking too long.** A diagnostic an operator abandons tells them nothing, so the
  extra work must be bounded.
- **Leftover objects.** Re-measuring means more Pods and policies; every one must still be
  cleaned up, including on the paths that return early.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The probe MUST NOT report "not enforced" on the strength of a single reach under
  policy.
- **FR-002**: A reach under policy MUST be re-measured before it becomes a verdict.
- **FR-003**: A re-measurement that is blocked MUST yield "enforced", and MUST say the policy
  took effect late.
- **FR-004**: A re-measurement that also reaches MUST yield "not enforced" — a true negative
  stays a true negative.
- **FR-005**: A re-measurement that cannot be taken MUST yield inconclusive, distinguishable
  from both verdicts.
- **FR-006**: The probe MUST still refuse to claim enforcement without a reachable control.
- **FR-007**: The additional work MUST be bounded, so the probe still terminates promptly.
- **FR-008**: Every object the probe creates MUST still be removed, including on early returns.
- **FR-009**: The verdict detail MUST state what was observed, including whether a
  re-measurement happened and what it found.
- **FR-010**: The live comparison test MUST NOT itself race, or it will disagree with a correct
  probe.

### Key Entities

- **Verdict**: enforced / not enforced / inconclusive, plus what was observed to reach it.
- **Measurement under policy**: one Pod's attempt to reach the control target with the deny-all
  in place. Now possibly taken more than once.
- **Settling**: the interval between a policy being accepted and being programmed. Not
  observable directly; only its consequences are.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A cluster whose policy takes effect after the first measurement is reported as
  enforcing.
- **SC-002**: A cluster that never enforces is still reported as not enforcing.
- **SC-003**: No verdict claims an observation that was not made.
- **SC-004**: The probe's worst-case duration stays bounded and is stated.
- **SC-005**: The probe leaves nothing behind, on every path including early returns.
- **SC-006**: The live test compares two measurements that are both settle-aware, so a correct
  probe cannot fail it.

## Out of Scope

- Changing what coordinare does with the verdict, or any performer isolation behaviour.
- Ingress enforcement.
- Supporting CNIs that enforce intermittently — that is reported, not accommodated.
