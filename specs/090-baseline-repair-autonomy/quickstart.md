# Quickstart — Baseline Repair Autonomy (spec-090)

Manual validation, layer by layer, matching the phased rollout in [plan.md](plan.md).
Every layer is **default-disabled**; with nothing enabled, coordinare behaves exactly as
pre-spec-090 (SC-006). Enable one layer at a time, per persona scope.

> Coordinare launch reminder: always `set -a && source .env && set +a` before starting,
> or `config.yaml` `${VAR}` placeholders silently expand to empty strings.
> Run tests with `.venv/bin/pytest`; lint with `.venv/bin/ruff check <files>`.

---

## Prerequisite for L3 — GitHub branch protection (FR-021, SC-005, research Decision 7)

L3 lands repair commits on the card's **existing active branch / open PR** and relies on
GitHub to invalidate any stale human approval when the repair pushes a new commit.
**Before enabling L3**, confirm on the target repo's protected branch:

- Branch protection rule exists for the PR's base branch.
- **"Dismiss stale pull request approvals when new commits are pushed"** is **ON**.
- At least one required approving review is configured.

Without this, a repair commit could land under a pre-existing approval and the
closer-boundary merge gate (`monitor_pr.py:221` `approved` precondition) would not force
a fresh human review. Coordinare does **not** re-implement this dismissal; it depends on
the native setting. The L3 dispatch comment restates this assumption.

---

## Layer 1 — Refuse to merge onto a red base (US1, P1)

**Config** (per persona scope):

```yaml
personas:
  <persona>:
    scopes:
      <scope>:
        baseline_prevention_gate:
          enabled: true
```

**Setup.** A PR that is itself green and approved, whose base branch has a **required**
check currently red.

**Steps.**
1. Drive the card to the closer boundary (`approved == true`).
2. Observe the merge decision.

**Expected.**
- Merge is **held**, not executed. State phase reflects a base-not-green hold; the hold
  records the failing **base** check name + URL (FR-003).
- Re-running the cycle while the base is still red holds again — **no latch** (FR-006).
- Make the base green, re-run → merge proceeds (FR-006, re-read each cycle).

**Negative / fail-safe.**
- Base branch protection unreadable or base rollup unfetchable → gate falls through to the
  head-only decision (does **not** hard-block) and emits the indeterminate signal
  (FR-005, SC-002).
- A base check that is red but **non-required** does **not** block (FR-004).

**Performance.** Enabled gate adds ≤500ms p95 (head+base fetched concurrently);
disabled adds nothing (SC, research Decision 2/8).

---

## Layer 2 — Classify a failure's origin (US2, P1, observe-only)

**Config:**

```yaml
        baseline_classification_gate:
          enabled: true
```

**Setup.** A PR whose head has at least one failing check that is **also** red on the
base branch for the **same reason**, plus (ideally) one failing check unique to the head.

**Steps.**
1. Let the performer boundary evaluate the CI gate.
2. Inspect the emitted `CIGateDecision` in the job's `capture_dir`.

**Expected.**
- The same-reason base failure appears in `inherited_checks` with
  `head_signature == baseline_signature`.
- The head-unique failure appears in `introduced_checks`.
- A transient-conclusion failure (e.g. `cancelled`, `timed_out`) appears in
  `flake_checks`, never `inherited_checks`.
- The **verdict is unchanged** vs. L2-disabled — classification is recorded only (SC-006).
- Exactly-one-classification holds: every `failed_checks` name is in exactly one list.

**Anti-masking check (SC-003).** Take an INHERITED case and change *only* the head
failure's reason (same check name, different error). It must reclassify to
**INTRODUCED** (different signature), not stay INHERITED.

**Indeterminate.** With the base rollup unavailable, head failures land in
`unknown_checks` (escalate-eligible), never `inherited_checks`.

**Performance.** Classification ≤50ms p95, pure CPU (research Decision 3).

---

## Layer 3 — Guarded autonomous repair (US3, P2, opt-in)

**Config:**

```yaml
        inherited_repair_gate:
          enabled: true
          max_repair_attempts_per_head: 1   # 0 = classify only, never dispatch
```

(Requires the L3 branch-protection prerequisite above. L2 classification must be
producing `inherited_checks` for repair to have anything to act on.)

**Setup.** A PR with an INHERITED stable failure (same signature on head and base),
on a repo with the stale-approval-dismissal protection ON.

**Steps.**
1. Reach the gate with `inherited_checks` non-empty and budget remaining.
2. Coordinare dispatches a baseline-repair job to the implementer on the **same active
   branch**, with `metadata["repair_mandate"]` set (see
   [contracts/repair-dispatch.md](contracts/repair-dispatch.md)).
3. The implementer produces a diff; the **dual test-integrity guard** evaluates it.

**Expected — happy path.**
- The repair fixes code/config; static guard `is_safe == True` **and** the `diagnostic`
  adversarial reviewer concurs → the change lands as a **candidate** on the open PR.
- `inheritance_repair_counter[head_sha]` increments at dispatch.
- The new commit **dismisses** the prior human approval (native branch protection);
  the merge gate then blocks until a **fresh human approval** (FR-021, SC-005). Coordinare
  never auto-merges the repair (FR-017).

**Expected — guard veto (SC-004, the safety-critical path).**
- A repair that only passes by weakening a test (deletes an assertion, adds
  `@pytest.mark.skip`/`xfail`, loosens a comparison, mocks away a requirement) is
  **rejected** by *either* guard half. The change does **not** land; phase →
  `blocked` with an `open_questions` entry + GitHub comment (FR-019, FR-020, FR-016).
- Uncertainty is treated as a veto (conservative). Verify each of the static corpus
  cases (remove assertion / add xfail-skip / loosen comparison / mock-away) is flagged.

**Expected — budget exhaustion (FR-022, FR-023).**
- With `max_repair_attempts_per_head: 1`, a second INHERITED failure on the same head
  does **not** dispatch again → phase `blocked` + `open_questions` + GitHub comment.
- `max_repair_attempts_per_head: 0` classifies/observes but never dispatches.

**Performance.** Repair + reviewer are out-of-band (30–90s); zero hot-path cost and zero
cost at defaults (research Decision 4/5).

---

## Operator signal — systemic base-fetch failure (FR-027)

Force repeated base-rollup fetch failures (e.g. revoke base read access transiently).
After ≥5 failures within a 1-hour window for a repo, coordinare emits a structlog
**error** `baseline_fetch_degraded`. Individual fetch failures still fail-safe
(L1 falls through to head-only); this is the aggregate operator alarm, not a per-PR block.

---

## State migration smoke (FR-026, SC-009)

1. Point coordinare at a snapshot written under schema v8 (no `inheritance_repair_counter`).
2. Load it.

**Expected.** Loads cleanly, migrated to v9, `inheritance_repair_counter == {}`. Empty
counter means **zero attempts taken**, not "unlimited" — the configured budget still
applies (FR-026). 100% of pre-feature snapshots load (SC-009).

---

## All-disabled baseline (SC-006)

With `baseline_prevention_gate`, `baseline_classification_gate`, and
`inherited_repair_gate` all absent or `enabled: false`, run an existing card end-to-end.
Routing, verdicts, and merge decisions must be **byte-identical** to a pre-spec-090
build. This is the headline guarantee of the phased rollout.
