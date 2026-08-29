# Implementation Plan: Forward External Issue Submissions

**Branch**: `152-forward-external-issues` | **Spec**: [spec.md](./spec.md) | **Issue**: [#210](https://github.com/ViviDynamics/coordinare/issues/210)

## Summary

One workflow on `issues: [opened]` that copies a bounded, verbatim extract of an outsider's issue
to every configured destination. No triage, no summary, no checkout.

## Technical Context

**Language**: workflow YAML + a small Python script. Tests are Python, in the existing unit suite.

**Dependencies**: none added. Stdlib `urllib` for delivery, so the script has no install step.

**Key decision — the logic is a script, not inline `actions/github-script`.** Every acceptance
criterion here is about *what gets sent*: verbatim fields, visible truncation, quoted presentation,
skip-vs-fail. Inline JavaScript in a workflow can only be tested by running the workflow, so those
criteria would be unverifiable until an outsider filed an issue. A script is callable from the unit
suite with a synthetic payload.

**Runner**: `ubuntu-latest`. Already used by `external-contributions.yml`, so it is available, and
an externally-triggered workflow should not touch the self-hosted host.

## Constitution Check

| Principle | Assessment |
|---|---|
| I. Code Quality | One script with one job. The membership check is *reused* from spec 142's reasoning rather than reinvented. |
| II. Testing | Tests precede the script. Delivery is injected so tests never make a network call. |
| III. UX Consistency | Reader can always tell the submitter's words from the workflow's — the same honesty rule as the rest of the repo's operator-facing output. |
| V. Observability | Skipped destinations are reported, not silent; that is FR-007's whole point. |

**Gate: PASS.**

## Structure

```
.github/workflows/forward-external-issues.yml   # trigger, membership gate, secrets -> env
.github/scripts/forward_external_issue.py       # extraction + delivery, testable
tests/unit/test_152_forward_external_issues.py
specs/152-forward-external-issues/
```

## Phases

1. **Tests first** — extraction verbatim-ness, truncation, quoting, skip vs fail, membership gate,
   workflow-shape assertions.
2. **The script** — extract, render per destination, deliver through an injected sender.
3. **The workflow** — `issues: [opened]`, membership gate mirroring spec 142, secrets to env, run
   the script. No checkout.
4. **Docs + verification** — CONTRIBUTING note that submissions are seen; full suite; commit.

## Complexity Tracking

| Item | Why not simpler |
|---|---|
| A script rather than inline `github-script` | Inline JS is only testable by filing a real issue, which makes every content criterion unverifiable before launch. |
| Two destinations rather than one | Requested. The cost is one loop and a per-destination skip; the alternative is revisiting the workflow when email is added. |

## Risks

- **The membership check under-reports private members** — the exact bug spec 142 hit, where a
  maintainer's own PR was scolded. Mitigated by using the permission-level API as the authority and
  the cheap field only as a pre-filter, mirroring 142.
- **A destination's markup could be abused by issue text.** Mitigated by quoting/escaping at the
  boundary and asserting it, rather than trusting the destination to be forgiving.
