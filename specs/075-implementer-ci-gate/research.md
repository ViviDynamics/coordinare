# Phase 0 Research: Implementer CI Gate

**Branch**: `075-implementer-ci-gate` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

## Why this gate exists

Today the implementer can mark itself terminal-success and the lifecycle advances to reviewer the moment the next graph tick reads `phase: reviewing`. The implementer's *internal* signal (it wrote code and pushed) is decoupled from whether the project's own CI accepts that code. We've seen three failure modes in live testing:

1. **Red CI handed to reviewer.** Reviewer is asked to review code the project's pipeline has already rejected — wasted reviewer cycles and confusing audit trail when the human eventually looks.
2. **Pending CI handed to reviewer.** Reviewer either pre-judges code that hasn't been validated, or has to abandon and resume — neither is what we want.
3. **Implementer "done" with no PR.** Already handled by 070's checkpoint logic; out of scope here (FR-013 defers).

The gate's job is one decision at one boundary: **at the implementer→reviewer transition, is the PR's CI green on the required check set?** Three answers — PASS (advance), HOLD (stay in `monitoring_performer`), BOUNCE (relay structured feedback back to implementer). A fourth, ESCALATE, exists only as a bounce-loop fuse (FR-015).

## Decision: reuse 064's primitives

064 (closer pr-checks gate) already shipped two reusable primitives:

- `services/pr_checks_service.py`: `CheckRollup` + `get_pr_check_rollup(pr_num)` — GraphQL fetch, capped at 100 checks, returns `CheckEntry` per check.
- `services/pr_checks_policy.py`: `GateDecision` enum + `decide(rollup, pending_timeout_seconds=900, treat_unknown_required_as="pass")` — encodes the fail-conclusion set `{failure, cancelled, timed_out, action_required, stale, startup_failure}` and the pending-timeout policy.

The right move is to **parameterize `decide()` by a required-checks list** rather than build a parallel decision engine. 064 evaluates "all checks present"; 075 evaluates "this card's required subset." Both gates use the same conclusion semantics, the same timeout policy, the same rollup fetch — the only difference is the filter applied before deciding. If `decide()` doesn't already accept a filter, the cleanest extension is an optional `required_check_names: set[str] | None` parameter that the caller populates from the resolver; absent argument retains 064's current behavior.

**Alternatives considered:**
- *Roll a separate gate engine.* Rejected — duplication of the fail-conclusion set + timeout policy is the exact "interface bloat over duplication" anti-pattern flagged in feedback_interface_first_design.
- *Have the resolver fetch the rollup itself.* Rejected — splits responsibility; rollup-fetching is 064's job, decision is 064's job, list-of-names is 075's job.

## Decision: deterministic resolver, not LLM-driven

074's `PersonaScope` was specifically designed to **not** include `required_checks` — the classifier sees deterministic inputs only (path stats, no diff body) and emits `{depth, focus, overrides}`. Adding `required_checks` to the classifier output would:

- Force the LLM to know GitHub check names (high-context, stack-specific, prompt churn).
- Make the gate decision non-deterministic across cycles (a classifier flip = different gate behavior on the same HEAD).
- Drag 074's classifier prompt schema into a new contract.

The resolver instead runs **deterministically** from operator config + the classifier's existing structured output:

```yaml
symphony:
  persona_scope:
    enabled: true
    persona_check_map:           # operator-defined, stack-specific, lives outside code
      implementer:
        skim:   ["lint*", "format*"]
        normal: ["lint*", "format*", "unit*"]
        full:   ["lint*", "format*", "unit*", "integration*", "e2e*"]
      reviewer:
        normal: ["lint*", "unit*"]
```

`RequiredChecksResolver.resolve(scope, branch_protection_set, all_head_checks)`:

1. If `persona_scope.persona_check_map` is configured AND `scope.personas["implementer"].depth` exists → use the map's pattern list, glob-match against `all_head_checks`. (FR-006a)
2. Else if branch-protection's required-status-checks set is non-empty → use that. (FR-006b)
3. Else → use the full `all_head_checks` set (matches 064's behavior — fail-safe permissive). (FR-006c)

The resolver returns a `set[str]` of concrete check names that exist on the current HEAD, which `decide()` then filters by.

**Alternatives considered:**
- *Hardcoded check name patterns in coordinare source.* Rejected — violates FR-008 / SC-005 (stack-agnostic).
- *Drive the list from the LLM.* Rejected per above.
- *Use `PersonaScope.focus` free-text to pattern-match check names.* Rejected — non-deterministic, fragile.

## Decision: BounceCounter as a bounce-loop fuse

A red CI that an implementer keeps failing to fix is a worse failure mode than "reviewer sees red CI" — it's an infinite loop. The fuse:

- `BounceCounter` is a dict on `CardSession`: `dict[head_sha, int]`.
- On every gate BOUNCE, increment `counter[current_head_sha]`.
- On every gate eval, if `counter[current_head_sha] >= max_bounces_per_head` (default 3, configurable), escalate to `needs_human_review` rather than bounce again.
- **Reset semantics**: a *new* HEAD (implementer pushed new commits) means the prior `head_sha` keys remain (audit trail) but the new `head_sha`'s counter starts at 0. This is the natural read of "reset on new HEAD" (FR-015) and gives operators a forensic trail of how many bounces happened per HEAD.
- Storage is a dict, not a single int + sha pair, specifically so the audit trail survives. The dict is bounded by the number of HEADs in the card's lifetime — small in practice.

**Alternatives considered:**
- *Reset the dict to `{}` on new HEAD.* Rejected — loses the audit trail.
- *Use a separate persisted log instead of a session field.* Rejected — adds a new persistence surface for ~3 ints per card.

## Integration points

| Boundary | File / function | Behavior |
|---|---|---|
| Gate evaluation | `graph/nodes/monitor_performer.py` lines ~1317–1359 (where `marker in TERMINAL_SUCCESS_STATES` advances stage) | Insert gate eval before `_advance_stage(state, status)`. PASS → existing advance; HOLD → return `{"phase": "monitoring_performer"}` (no change); BOUNCE → compose relay_feedback + return HOLD; ESCALATE → set `needs_human_review` flag. |
| Required-checks resolution | New `services/required_checks_resolver.py` | Pure function: `(scope, branch_protection_set, all_head_checks, persona_check_map) -> set[str]`. No I/O — caller fetches inputs. |
| Check rollup fetch | `services/pr_checks_service.get_pr_check_rollup(pr_num)` from 064 | Reused unchanged. |
| Decision policy | `services/pr_checks_policy.decide(rollup, ...)` from 064 | Extended with optional `required_check_names: set[str] \| None` parameter; existing callers default to None (preserves 064 behavior). |
| Branch-protection fetch | `services/github_service.py` (existing) | New helper if not present: `get_required_status_checks(default_branch)` — cached per-cycle. |
| Bounce dispatch | `graph/nodes/relay_feedback.py` from 070 | Reused unchanged. Gate BOUNCE appends a structured entry to `session["relay_feedback"]`. |
| Bounce counter | New field on `CardSession` (session.py TypedDict + `_SESSION_FIELDS`) | Round-trip via canonical pattern; schema v4→v5 bump on `PersistedSession`. |
| PR rollup comment | `graph/nodes/notify.py` (extended) | New rollup type `ci_gate_decision`, deduped by `(head_sha, decision-signature)` mirroring 064's `_persona_scope_signature` pattern. |

## Fallback posture

Matches 074's fail-open posture: any unexpected exception in gate eval is caught, logged at WARN with `error` + `error_type`, and the function returns the no-op path (let the existing implementing→reviewing transition proceed). This is intentional and tested — FR-011 explicitly requires the gate to never block the pipeline on its own failure. The cost of a missed gate eval is a reviewer seeing red CI once; the cost of a stuck pipeline is much higher.

## Open questions resolved

- **Q: Should the gate also fire at reviewer→closer?** No — 064 already gates at closer→merge. Two gates suffice for the lifecycle. Adding a third at reviewer→closer would duplicate 064's signal without new information.
- **Q: Should `BounceCounter` count across HEADs?** No — a new HEAD means the implementer responded to feedback; that is the signal that progress is happening. Reset (per-HEAD scoping) is the right policy.
- **Q: What about a check that's *required* by branch protection but absent on HEAD?** `decide()` with `treat_unknown_required_as="pass"` (the 064 default) covers this. If the operator wants strict mode, that's a 064-side toggle, not 075's concern.
- **Q: Does the gate run on every implementer turn or only at handoff?** Only at handoff — when `marker in TERMINAL_SUCCESS_STATES`. Per-turn evaluation would burn API calls for no signal change.
