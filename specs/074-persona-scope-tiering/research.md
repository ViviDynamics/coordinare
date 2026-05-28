# Phase 0 Research: Persona Scope Tiering

## R1 — Classifier input: path stats vs. raw diff

**Decision**: Pass deterministic path stats + the project's path-class config + the project's CLAUDE.md/AGENTS.md context. **Never pass the raw diff body.**

**Inputs the classifier sees**:
- File-level summary: `[{ path: "src/auth/middleware.py", added: 12, removed: 3, status: "modified" }, ...]`
- Project path-class config (resolved globs → class names)
- Project's `CLAUDE.md` and any `AGENTS.md` paths under the symphony root (truncated to a configurable header — default first 4 KB each)
- Card title and one-sentence summary (already on the card)

**Rationale**:
- **Token bound**: prompt size scales with file count, not diff size — predictable cost regardless of card. A 5000-line generated migration costs the same to classify as a 5-line typo fix.
- **Privacy**: raw diff contents may include secrets, customer data, or proprietary code paths. Path stats are diff-free by construction.
- **Determinism on retry**: same inputs produce same prompt; the classifier's output variance is bounded by the LLM, not by sampling diff snippets.
- **Stack-agnostic**: file paths are universal; diff syntax (Python vs. Go vs. Ruby) is not. Letting the classifier reason about paths and counts keeps it portable.

**Alternatives rejected**:
- *Pass the diff body*: prompts get huge fast, leaks risk, can't cap cost without truncation heuristics that hide signal.
- *Pass only path list (no LOC)*: loses the size dimension — can't distinguish a 6-line auth tweak (skim-candidate) from a 600-line auth rewrite (full-required).
- *Project ships its own classifier code*: defeats the stack-agnostic promise; each project would re-implement.

## R2 — Classifier output: free-text `focus` vs. structured tags

**Decision**: Free-text string, one to three sentences per persona (FR-013).

**Rationale**:
- **No vocabulary to maintain**: structured tags require coordinare to ship and version an enum the LLM is constrained to. Free-text lets the classifier produce specific guidance ("focus on token-refresh edge cases — there's a new flow in `auth/refresh.py:88`") that a fixed tag list can't express.
- **User-friendly to author**: project authors don't need to memorize tag vocabulary to predict classifier output.
- **Future-compatible**: a structured-tag layer can be added on top later without breaking existing scopes (the `focus` field stays as a string; tags become a separate optional field). The reverse (start structured, loosen later) is much harder — every project's existing automation breaks.

**Mitigation for the looseness**: the classifier prompt instructs "one to three sentences, plain prose, no bullet lists." The `overrides` field on `PersonaScope` carries the *structured* reason for any forced-full or skip decision so the deterministic axis is still machine-consumable.

## R3 — Classifier runs on which backend?

**Decision**: Reuse coordinare's existing `conducting` block (FR-014). No new `classifier_backend` config slot.

**Rationale**:
- Operators already configure exactly one "internal brain" backend (`ConductingConfig` at `src/coordinare/config.py:216`). Adding a second slot doubles the configuration surface for marginal benefit — the classifier's prompt is short, deterministic, and bounded; it's the same kind of work the conducting brain does today.
- The existing `build_conducting_backend()` factory at `src/coordinare/services/conducting.py:598` already abstracts over six backends (anthropic_api, openai_api, claude_cli, codex_cli, opencode, null). The classifier service uses the same factory output.
- If an operator needs a *different* model for classification specifically, they can layer a LiteLLM proxy in front and route per-purpose there. That's a generic capability — not something to bake into coordinare.

**Alternatives rejected**:
- Dedicated `classifier_backend` block: rejected per FR-014; doubles config surface for marginal benefit.
- Hardcode a cheap-and-fast model (Haiku/gpt-4o-mini): rejected — defeats stack-agnostic promise; operators on air-gapped or self-hosted setups can't use it.

## R4 — Recompute cadence: every cycle vs. once at dispatch

**Decision**: Every cycle (FR-003).

**Rationale**:
- A card's diff can grow substantially between cycles (implementer comes back with a much larger change). Classifying once at first dispatch and locking the scope risks under-scoping the persona reviews that matter most — exactly the cards we'd most regret being shallow on.
- Per-cycle cost is bounded by R1's design (path stats only, not diff). At SC-006's 2 s p50 / 10 s p95 budget, the per-cycle overhead is well below the cycle's typical tool-call latency.
- Fallback (FR-006): if a recompute fails, the previous cycle's `PersonaScope` is reused with a debug log, so transient classifier failures don't regress to "no scope" — they regress to "yesterday's scope." This is strictly better than "no recompute."

**Alternative rejected**:
- *Recompute only when the diff's file list changes*: optimization that breaks under the FR-006 fallback (a stale `PersonaScope` whose file list didn't change but whose LOC totals doubled would be wrong). Correctness > marginal savings.

## R5 — Where in the graph does the classifier run?

**Decision**: New node `classify_scope` between `dispatch_card` and `dispatch_performer` in `graph/builder.py`. Skip-routing handled in `graph/routing.py` (depth=`skip` advances the lifecycle without invoking the performer).

**Rationale**:
- `dispatch_card` is the existing pickup point that finalizes which card the cycle works on; the scope must be computed *after* this so the classifier knows which card to look at.
- `dispatch_performer` is where the per-persona invocation happens; the scope must be available *before* this so the slice can be threaded into the invocation.
- Inserting between these two is the natural seam — no existing edge is broken; the new node is invoked unconditionally each cycle (per FR-003).
- Skip-routing in `routing.py` is cleaner than gating in `dispatch_performer` because the existing routing decision tree already has the "advance stage, no performer" pattern (used by the assess-skip path on PR-already-open at `pickup_skips_assess_when_pr_open`).

## R6 — Failure modes and fallbacks

| Failure | Behavior |
|---|---|
| Classifier backend network error | Full-depth-everywhere; warning logged + Slack post (rate-limited). FR-006. |
| Classifier backend timeout (>budget) | Same as network error. Budget configurable, default 30 s. FR-016. |
| Classifier returns malformed JSON | Same as network error. |
| Classifier output schema-valid but unknown persona name | Ignore unknown persona (debug log); honor remaining. FR-015. |
| Classifier output schema-valid but unknown depth value | Treat as malformed → full-depth-everywhere fallback. |
| No PR open yet (mid-implementer, no diff) | Skip classifier; default to full-everywhere. No warning. (Edge case in spec.) |
| Card has `forced_full_on_path_classes` match | Override the classifier's output for the listed personas to `full`; record `overrides: ["forced_full_on_path_class:<class>"]`. FR-005. |
| Empty `_SESSION_FIELDS` on snapshot rehydrate (v1/v2/v3 → v4) | Treat as "not yet computed"; next cycle recomputes. FR-011. |

## R7 — PR-visible surface for scope rollup

**Decision**: PR comment, posted once when `PersonaScope` is first computed per card (or when it materially changes between cycles — depth shift or persona added/removed). Uses the existing PR-comment channel that 064's pr-checks rollup uses. Comment is de-duplicated by a "marker" string so repeated rollup edits replace the previous comment rather than stacking.

**Rationale**: SC-004 requires reviewers to see depth + focus without reading logs. PR comments are universally visible to humans reading the PR, integrate with GitHub notifications, and reuse the existing rollup mechanism. Status-line / check-run alternatives would require new infrastructure for a marginal UX gain.

## R8 — Stack-agnostic via path globs

**Decision**: All path classification is glob-based against a project-supplied taxonomy. Coordinare ships **no default globs**.

**Rationale**: FR-004 / SC-005. A Rails app's "config" is `config/**/*.rb`; a Go service's is `*.toml`. Coordinare cannot guess. The quickstart documents a recommended starter taxonomy for the four most common stacks (Python/Node/Go/Rails) but each is project-owned.

**Implementation**: `PersonaScopeConfig.path_classes: dict[str, list[str]]` — class name → list of globs. Match precedence: a file matches every class whose glob matches; the classifier sees the full set of class memberships per file.

## R9 — Closer scope-invariance

**Decision**: Closer always ignores `depth` and consumes only `focus` (FR-009). A project config that sets `closer.scope_behavior.respects_depth: true` is ignored with a startup warning.

**Rationale**: Closer is a determinism check (CI passed, branch is mergeable, no unresolved review threads). Running it at "skim" depth doesn't save meaningful effort — the work is mostly API calls, not model reasoning. Letting projects override this would create configurations where the merge gate is shallower than the review depth, which is the wrong polarity (the merge gate should be the strictest, not the most negotiable).

## R10 — Schema version bump

**Decision**: Bump `CURRENT_SCHEMA_VERSION` 3 → 4 for `WorkflowSnapshot`. `MIN_SUPPORTED_SCHEMA_VERSION` stays at 1. v1–v3 snapshots rehydrate with `persona_scope = None` on each `PersistedSession` and recompute on the next cycle. No migration tooling required (FR-011 graceful upgrade).
