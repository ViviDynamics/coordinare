# Quickstart: Adopting Persona Scope Tiering

This guide walks a project from "feature off" to "feature fully wired" in three stages: **shadow mode** (classifier runs, no persona behavior change), **selective enforcement** (one or two personas honor the classifier), and **full adoption** (all personas tiered + forced-full risk overrides). At every stage, the feature is **opt-in** — omitting the config blocks below leaves your project running exactly as it does today.

## Prerequisites

- coordinare schema v4 or newer (set automatically on first save after this feature lands; v1–v3 snapshots upgrade transparently on the next cycle — FR-011).
- A `conducting` backend already configured (`symphony.conducting.*`). The classifier runs on the same backend; no new credentials needed (FR-014).
- A project `CLAUDE.md` and/or `AGENTS.md` at the symphony root. The classifier reads the first 4 KB of each as context (R1). Projects without these still work — the classifier just has less context.

## Stage 1 — Shadow mode (no behavior change)

Goal: see what the classifier *would* say without changing how any persona runs. Useful for tuning the path-class taxonomy before personas start consuming the output.

Add to your coordinare config:

```yaml
symphony:
  persona_scope:
    enabled: true
    path_classes:
      docs:               ["*.md", "docs/**/*"]
      config:             ["config/**/*.yaml", "*.toml"]
      tests:              ["tests/**/*", "**/test_*.py"]
      runtime:            ["src/**/*.py"]
      security_sensitive:
        - "src/auth/**/*.py"
        - "**/migrations/**"
```

That's it. No persona has a `scope_behavior` block yet, so every persona continues to run with today's behavior. The classifier still runs on every cycle and writes a `PersonaScope` to the session — visible via:

- The `persona_scope.classifier.complete` structlog event (`depths={persona:depth}`).
- The PR-comment rollup posted by `notify` (FR-012 / SC-004).
- The `persona_scope` field on persisted sessions in `coordinare.state.json`.

Tune `path_classes` until the rollup comments look right for several representative cards. Iterate without risk — nothing downstream changes.

## Stage 2 — Selective enforcement (one persona)

Goal: let one persona honor the classifier. Start with `tech_writer` (lowest blast radius — wrong calls just mean docs-review effort, not security gaps).

```yaml
symphony:
  personas:
    tech_writer:
      instructions: "..."
      scope_behavior:
        skim:   { max_tool_calls: 3,  prompt_addon: "Verify docs are not stale." }
        normal: { max_tool_calls: 10, prompt_addon: "" }
        full:   { max_tool_calls: 25, prompt_addon: "Comprehensive docs review." }
```

Now on a docs-only PR, the classifier will likely emit `tech_writer: full` (because docs are tech_writer's core concern) and other personas `skim` — but only `tech_writer` changes behavior. The others still run as today (FR-010 additive default).

`depth: skip` short-circuits the persona at the routing layer — the lifecycle advances past it without invocation (R5).

## Stage 3 — Full adoption + risk overrides

Goal: every persona is tiered, and a deterministic safety net forces `full` for risk-class paths regardless of the classifier's call.

```yaml
symphony:
  persona_scope:
    enabled: true
    path_classes:
      docs:               ["*.md", "docs/**/*"]
      config:             ["config/**/*.yaml"]
      tests:              ["tests/**/*", "**/test_*.py"]
      runtime:            ["src/**/*.py"]
      security_sensitive:
        - "src/auth/**/*.py"
        - "src/coordinare/services/github.py"
        - "**/migrations/**"
    forced_full_on_path_classes:
      security: ["security_sensitive"]
      reviewer: ["security_sensitive"]
    classifier_latency_budget_seconds: 30.0

  personas:
    reviewer:
      scope_behavior:
        skim:   { max_tool_calls: 5,  prompt_addon: "Spot-check correctness only." }
        normal: { max_tool_calls: 15, prompt_addon: "" }
        full:   { max_tool_calls: 30, prompt_addon: "Check invariants and edge cases." }
    security:
      scope_behavior:
        skim:   { max_tool_calls: 5,  prompt_addon: "Sanity check; flag any risk you see." }
        normal: { max_tool_calls: 15, prompt_addon: "" }
        full:   { max_tool_calls: 40, prompt_addon: "Threat-model the change." }
    qa:
      scope_behavior:
        skim:   { max_tool_calls: 3,  prompt_addon: "Smoke-test only." }
        normal: { max_tool_calls: 15, prompt_addon: "" }
        full:   { max_tool_calls: 40, prompt_addon: "Full regression sweep on affected areas." }
    tech_writer:
      scope_behavior:
        skim:   { max_tool_calls: 3,  prompt_addon: "Verify docs are not stale." }
        normal: { max_tool_calls: 10, prompt_addon: "" }
        full:   { max_tool_calls: 25, prompt_addon: "Comprehensive docs review." }
    closer:
      instructions: "..."
      # NO scope_behavior — closer is scope-invariant (FR-009). If you set one, it is
      # ignored with a startup warning. Closer always runs full, but DOES consume the
      # classifier's `focus` field as advisory context.
```

Now: a 6-line change to `src/auth/middleware.py` triggers `forced_full_on_path_classes` for `security` and `reviewer`, so both run `full` regardless of what the classifier said. The `overrides` field on the scope slice records `"forced_full_on_path_class:security_sensitive"` for auditability.

## Recommended starter taxonomies by stack

| Stack | `runtime` | `tests` | `config` | `security_sensitive` (suggested) |
|---|---|---|---|---|
| Python | `src/**/*.py` | `tests/**/*`, `**/test_*.py` | `*.toml`, `config/**/*.yaml` | `src/auth/**/*.py`, `**/migrations/**` |
| Node | `src/**/*.{ts,tsx,js}` | `test/**/*`, `**/*.test.{ts,js}` | `*.json`, `config/**/*` | `src/auth/**`, `src/middleware/auth*` |
| Go | `**/*.go` (excl. `_test.go`) | `**/*_test.go` | `*.toml`, `*.yaml` | `internal/auth/**`, `cmd/*/main.go` |
| Rails | `app/**/*.rb` | `spec/**/*`, `test/**/*` | `config/**/*.rb`, `*.yml` | `app/controllers/sessions*`, `db/migrate/**` |

Coordinare ships none of these — they're starter examples. Copy what fits, edit liberally (FR-004 / SC-005).

## Validating the rollout

After Stage 1 deploys:

1. Watch a few cards land. Check the PR-rollup comment on each — does the per-persona `depth` and `focus` match what you'd manually decide? If not, adjust `path_classes` (most miscalls come from missing or over-broad globs).
2. Check `persona_scope.classifier.failed` log frequency. If non-trivial, increase `classifier_latency_budget_seconds` or check the `conducting` backend's health.
3. Once Stage 1 looks good for 1–2 weeks, advance to Stage 2.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Classifier rollup comment never appears on PRs | `persona_scope.enabled` is `false` (or block missing) | Set `enabled: true` and define `path_classes`. |
| Startup warning "persona_scope enabled but path_classes empty" | `enabled: true` with no globs | Add globs under `path_classes`, even if just one class. |
| Startup error referencing `forced_full_on_path_classes[<persona>]` | Override references a class not defined in `path_classes` | Either define the class or remove it from the override list. |
| Startup warning "closer.scope_behavior ignored" | A `scope_behavior` block was set on `closer` | Remove it (FR-009). Closer is scope-invariant by design. |
| Persona's `max_tool_calls` doesn't appear to be honored | The persona has `scope_behavior` but `persona_scope.enabled = false` | Enable `persona_scope` and define `path_classes` — `scope_behavior` only takes effect when the classifier runs. |
| Every persona runs `full` after a transient backend hiccup | FR-006 fallback engaged | Expected. Check `persona_scope.classifier.failed` logs; once the backend recovers, the next cycle reclassifies. |
| v1/v2/v3 snapshot just loaded — no `persona_scope` on session | Expected per FR-011 graceful upgrade | The next cycle recomputes; no action needed. |

## What you do NOT need to do

- No migrations. Schema bumps to v4 transparently on first save.
- No code in your project. The taxonomy is YAML; the classifier prompt is shipped by coordinare.
- No new backend credentials. The classifier reuses your existing `conducting` backend (FR-014).
- No retroactive scope on existing sessions. Each session recomputes on its next cycle.
