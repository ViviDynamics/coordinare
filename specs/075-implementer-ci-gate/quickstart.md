# Quickstart: Adopting the Implementer CI Gate

**Branch**: `075-implementer-ci-gate` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

This guide is for operators adopting 075 on an existing coordinare-managed repo. It assumes 064 (closer pr-checks gate) and 074 (persona scope tiering) are already shipped in your coordinare version (they are, on `main` as of 2026-05-27).

---

## TL;DR

```yaml
# config.yaml — add to your existing symphony block
symphony:
  persona_scope:
    enabled: true              # if not already enabled, enable it
    ci_gate:
      enabled: true            # turn the gate on
      max_bounces_per_head: 3  # default
```

Restart coordinare. From the next card forward, when the implementer marks itself done, coordinare checks the PR's CI before handing off to reviewer. Red CI → bounce back to implementer with structured feedback. Green CI → advance as before.

That's the minimum viable adoption. Read on for tuning.

---

## Step 1: Verify prerequisites

```bash
# coordinare must be on a build that includes 075
.venv/bin/python -c "from coordinare.services import required_checks_resolver; print('ok')"

# the PR you're testing on must have at least one CI check on its HEAD commit
GH_TOKEN=$(cat ../.gh_token) gh pr view <PR#> --json statusCheckRollup
```

If the PR has zero CI checks, the gate falls through to "all checks on HEAD" which is the empty set — gate PASSes vacuously. This is intentional (fail-safe permissive) but defeats the point.

---

## Step 2: Decide your required-checks source

You have three options, in increasing specificity:

### Option A: "use whatever's required by branch protection" (recommended for most repos)

Set `ci_gate.enabled: true` and leave `persona_check_map` unset. The resolver will:

1. Look for `persona_check_map` (not set → skip).
2. Look up the default branch's required-status-checks (GitHub branch protection).
3. Fall back to all checks on HEAD if branch protection is empty.

This is the lowest-friction path and matches the intuition most teams have: "the gate enforces what we already told GitHub is required."

```yaml
symphony:
  persona_scope:
    enabled: true
    ci_gate:
      enabled: true
```

### Option B: per-persona-per-depth check map (recommended when running 074 in earnest)

If 074 is meaningfully tiering personas (some cards scope to `skim`, others to `full`), you can match the gate's stringency to the scope depth. A docs-only card might only require lint; a security-sensitive card requires the full integration suite.

```yaml
symphony:
  persona_scope:
    enabled: true
    persona_check_map:
      implementer:
        skim:   ["lint*"]
        normal: ["lint*", "unit-tests*"]
        full:   ["lint*", "unit-tests*", "integration*", "e2e*"]
    ci_gate:
      enabled: true
```

Patterns are shell globs (`fnmatch`). The resolver intersects them with the actual checks present on the PR's HEAD — patterns that match nothing are silently dropped (they don't cause the gate to fail).

### Option C: both (advanced)

If `persona_check_map` is set and produces a non-empty intersection, that wins. Otherwise the resolver falls through to branch-protection, then all-checks. Use this if you want per-persona stringency but a sensible fallback when persona scope is unavailable (e.g. card has no PR yet, persona absent from map).

---

## Step 3: Tune `max_bounces_per_head`

Default is 3. The counter is per-HEAD: every time the implementer pushes new commits, the counter for the new SHA starts at 0. Prior HEAD entries are preserved on the session for audit but don't count against the new HEAD.

- Lower (1–2): aggressive escalation to human review — good if you don't trust the implementer to self-recover on CI feedback.
- Higher (5+): more patience — good if your CI is itself flaky and a "bounce" is sometimes the right answer just so CI re-runs.

When the limit is hit, coordinare sets `phase: needs_human_review` and posts a rollup comment on the PR. The card stops moving until a human resolves it.

---

## Step 4: Inspect the gate in action

The gate posts a PR comment per *distinct decision* on each HEAD — passes, holds, bounces, escalations each get a comment if they're new. Look for the dedup marker `<!-- coordinare:ci-gate:<signature> -->` in PR comment HTML.

Structured logs (look for `event=ci_gate.decided` in stdout):

```json
{
  "event": "ci_gate.decided",
  "card_id": "VVD-1234",
  "verdict": "bounce",
  "head_sha": "a1b2c3d4...",
  "required_checks": ["lint", "unit-tests"],
  "failed_checks": ["unit-tests"],
  "bounce_count_after": 1,
  "resolver_source": "branch_protection"
}
```

The `resolver_source` field tells you which fallback layer produced the decision — useful when tuning the map.

---

## Step 5: Disabling

Two ways to disable:

1. **Per-project**: set `symphony.persona_scope.ci_gate.enabled: false`. Lifecycle reverts to pre-075 behavior immediately on next cycle. The `BounceCounter` state persists on existing sessions but stops being checked.
2. **Per-card**: not currently supported via config; for an emergency override on one card, manually advance `phase` in the persisted session JSON. (Not recommended; better to set `max_bounces_per_head` aggressively.)

---

## Troubleshooting

**The gate seems to always PASS even with red CI.**
Check `event=ci_gate.decided` log entries. If `required_checks` is empty, the resolver fell through to all three layers and found nothing. Either set `persona_check_map`, enable branch protection on the default branch with required status checks, or both.

**Implementer keeps bouncing on the same failure.**
Look at `bounce_count_after` in logs. Once it hits `max_bounces_per_head`, you'll see `verdict=escalate` and the card moves to `needs_human_review`. If it's bouncing infinitely without escalating, that's a bug — check that the implementer is actually consuming `relay_feedback` entries (070 should be in place).

**v4 → v5 schema migration: anything to do?**
No. v4 snapshots load with `bounce_counter={}` and the gate populates it on first decision. No manual migration step required.

**My CI uses GitHub Actions matrix builds with dynamic names.**
Use glob patterns: `"build (3.11, ubuntu)*"` or just `"build*"` covers all matrix legs. The resolver intersects with concrete check names on HEAD, so the pattern fans out correctly.

---

## What's not covered here (yet)

- Reviewer-side bounce policy lives in the existing reviewer prompt; 075 only touches the implementer→reviewer boundary.
- 064's closer→merge gate is unchanged and continues to operate at the merge boundary.
- Notification routing (Slack, etc.) is outside 075 — the PR comment is the canonical surface.
