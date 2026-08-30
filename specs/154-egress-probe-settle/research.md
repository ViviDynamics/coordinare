# Research: A settle-aware egress probe (spec 154, issue #233)

Verified against `main` at 2e65589 on 2026-08-30.

## R1 — Why not wait a fixed period after applying the policy

**Decision**: rejected as the primary mechanism.

A `sleep` after `create_namespaced_network_policy` is a guess about how long someone else's CNI
takes to program a rule, on hardware we have never seen. Too short and the race survives, which
is the whole defect; too long and every `coordinare doctor` run pays for it on every cluster,
including the overwhelming majority where the policy was programmed immediately.

It also cannot be validated. There is no observation that says "the sleep was long enough" —
only the measurement itself says that, which is R2.

## R2 — Re-measure instead

**Decision**: never conclude "not enforced" from a single reach under policy. A reach is
re-measured with a second Pod; only a reach that survives re-measurement is a verdict.

**Rationale**: the two cases the race confuses are distinguishable *over time* and not at a
point. A cluster that does not enforce lets traffic through every time; a cluster whose policy
had not landed yet lets it through once. Sampling twice separates them, and does so without
anyone having to know the CNI's programming latency in advance.

It also costs nothing in the common cases. A cluster that enforces blocks on the first
measurement and never re-measures; a cluster that does not enforce pays one extra Pod, once, in
a diagnostic an operator ran deliberately.

**Bound**: exactly one re-measurement (FR-007). A loop would turn "does not enforce" into a
timeout, and "your cluster does not enforce NetworkPolicy" is a true and useful answer that must
stay fast (FR-004, SC-002).

**Alternatives considered**:
- *Poll until the policy is observably in effect, then measure.* The observation and the
  measurement are the same experiment, so this is not a precondition — it is R2 with extra
  words.
- *Read the CNI's own state.* Every CNI reports differently, and inferring enforcement from a
  CNI's name or status is exactly what this probe exists to replace.

## R3 — Which direction the error runs, and why it still matters

The probe already refuses to claim enforcement it has not proven: no reachable control means
inconclusive, never "enforced" (`kubernetes_egress.py:324-335`). Telling an operator their
performers are contained when they are not is the failure that must never be guessed, and this
race does not cause it.

This race causes the opposite: reporting no protection where there is some. Worth fixing anyway.
`coordinare doctor` is the surface an operator uses to decide whether the cluster is safe to run
AI-generated code in (`doctor.py:287, 326`). An operator told their cluster ignores
NetworkPolicy will either build something else or trust nothing, and both are expensive
responses to a wrong answer.

## R4 — Cleanup

**Decision**: no change needed, and confirmed rather than assumed.

`_create_pod` appends every Pod it creates to `created.pods`, and `_cleanup()` runs in a
`finally` over that list. A second denied Pod is therefore removed by the existing machinery,
including on the early returns. The re-measurement gets its own unique name for the same reason
the originals do — a deterministic name collides with the previous run's Pod while it is still
Terminating, which is the trap spec 146 hit.

## R5 — The live test races the same way

**Decision**: fix it too, or a correct probe still fails it.

`tests/integration/test_225_egress_probe_live.py` asserts `verdict.enforced == manually_blocked`
where `manually_blocked` comes from applying `manual-deny` and immediately running
`manual-probe`. That hand-run comparison has precisely the race the probe has, so the two can
disagree while the probe is right — and the recorded failure had the probe reporting
not-enforced, which is the shape this race produces on both sides.

Making the manual side re-measure the same way keeps the test an independent check rather than
a copy of the implementation: it still applies its own policy and runs its own Pod, and only the
sampling discipline is shared.

## R6 — What the operator is told

**Decision**: three distinguishable details, one per outcome (FR-009).

A probe that quietly retried would be a probe nobody could audit — the operator would see
"enforced" with no way to know it took two attempts. "Blocked on the first attempt" and "got
through once, then blocked" are different facts about their cluster, and the second is worth
knowing: it means anything measuring enforcement immediately after applying a policy will get
the wrong answer there.
