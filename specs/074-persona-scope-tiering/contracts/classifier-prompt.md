# Contract: Persona Scope Classifier Prompt

This document is the **authoritative contract** between coordinare and the classifier LLM. Changes here must be reviewed; downstream implementations must conform.

## Inputs (rendered into the user message)

The classifier is called via `ConductingBackend.prompt(system=..., user=..., response_format="json")` using coordinare's existing `conducting` block (FR-014). Coordinare renders the user message from the deterministic inputs below — **never from the raw diff body** (FR-002).

### Input schema (JSON, embedded in the user message)

```json
{
  "card": {
    "id": "PVI_42",
    "title": "Add token-refresh edge-case handling",
    "summary": "Refactor session.refresh() to retry once on stale-credential errors."
  },
  "pr": {
    "number": 91,
    "head_sha": "abc123...",
    "files": [
      { "path": "src/coordinare/auth/session.py", "added": 12, "removed": 3, "status": "modified", "classes": ["runtime", "security_sensitive"] },
      { "path": "tests/unit/test_session.py",   "added":  8, "removed": 0, "status": "modified", "classes": ["tests"] }
    ]
  },
  "project_context": {
    "claude_md_head":    "...first 4 KB of CLAUDE.md...",
    "agents_md_head":    "...first 4 KB of AGENTS.md (if present)..."
  },
  "personas": ["reviewer", "security", "qa", "tech_writer", "closer"],
  "path_classes": {
    "docs":               ["*.md", "docs/**/*"],
    "config":             ["config/**/*.yaml"],
    "tests":              ["tests/**/*", "**/test_*.py"],
    "runtime":            ["src/**/*.py"],
    "security_sensitive": ["src/auth/**/*.py"]
  },
  "depth_definitions": {
    "skim":   "Spot-check; look for obvious correctness or risk issues only. ~5 tool calls.",
    "normal": "Standard review for this persona. ~15 tool calls.",
    "full":   "Thorough investigation; check invariants, edge cases, integration points. ~30+ tool calls.",
    "skip":   "Do not run this persona. Use only when the change has no surface area for this persona."
  }
}
```

## Output schema

```json
{
  "personas": {
    "reviewer":    { "depth": "skim|normal|full|skip", "focus": "<1-3 sentences>" },
    "security":    { "depth": "...", "focus": "..." },
    "qa":          { "depth": "...", "focus": "..." },
    "tech_writer": { "depth": "...", "focus": "..." },
    "closer":      { "depth": "...", "focus": "..." }
  }
}
```

**Constraints**:
- Every persona listed in `personas` (input) MUST appear in `personas` (output). Missing personas are treated as `depth: full` (defensive default).
- Unknown depth values trigger the FR-006 fallback (full-depth-everywhere).
- Unknown persona keys (i.e., a persona the classifier emits but coordinare didn't ask for) are ignored with a debug log (FR-015).
- `focus` MUST be one to three sentences of plain prose. No bullet lists, no JSON-nested structure inside the string.
- The output MUST NOT include `overrides` — that field is reserved for coordinare-applied structured reasons (e.g., forced-full path-class matches) and is appended post-classifier.

## System prompt (template)

```
You are coordinare's persona scope classifier. For a single pull request, decide
how much attention each downstream persona (reviewer, security, qa, tech_writer,
closer) should pay to the change.

You will receive:
  - the card's title and summary
  - a per-file summary of the diff (path, ±LOC, status, path-class memberships)
  - the project's path-class taxonomy (which globs count as docs, config, tests,
    runtime, security_sensitive, etc.)
  - excerpts from the project's CLAUDE.md and AGENTS.md to understand conventions

You will NEVER see the raw diff body. Reason from paths, sizes, and project
context only.

For each persona, output:
  depth: one of "skim", "normal", "full", "skip"
  focus: one to three sentences of plain prose — what should this persona pay
         attention to on this specific change? Be concrete. Reference file
         paths or behaviors by name when helpful. Do not write bullet lists.

Guidelines:
  - Default to "normal" when uncertain. Use "skim" only when the persona's
    domain is genuinely orthogonal to the change (e.g., security on a docs-only
    PR). Use "full" when the change touches the persona's core concern (e.g.,
    security on an auth file). Use "skip" sparingly — only when the change has
    zero surface area for that persona.
  - The "closer" persona's depth field will be ignored — closer always runs.
    Still emit a depth for it (any value is fine); the focus field IS read by
    closer as advisory context, so write it carefully.
  - Path-class membership is a strong signal but not absolute. A 5-line change
    in a security-sensitive file may still warrant "full" for security and
    "skim" for tech_writer.
  - Test-only changes (only `tests` class touched) typically warrant skim for
    reviewer, skip for security/qa, and skip for tech_writer.
  - Documentation-only changes (only `docs` class touched) typically warrant
    skim for reviewer, skip for security/qa, and full for tech_writer.

Respond with a single JSON object matching the output schema. Do not include
any prose outside the JSON.
```

## User message (template)

The user message embeds the input JSON wrapped in a brief framing string so the model treats it as structured input rather than free-form text.

```
Classify the following pull request:

<input>
{INPUT_JSON}
</input>

Output the JSON object now.
```

## Post-processing (coordinare-side)

1. Parse JSON. If parse fails → FR-006 fallback (full-depth-everywhere, warning).
2. Validate against the output schema. If validation fails → FR-006 fallback.
3. For each persona in `forced_full_on_path_classes`, check if any file's class memberships intersect the configured list. If yes → override that persona's depth to `full` and append `"forced_full_on_path_class:<class>"` to its `overrides`.
4. For `closer`, force-append `"closer_is_scope_invariant"` to `overrides` and document that the depth field will be ignored at dispatch time.
5. For any persona key in the LLM output that's not in `personas` (input) → drop with a debug log (FR-015).
6. For any persona in `personas` (input) missing from LLM output → fill with `depth: full, focus: "(no classifier output — defaulting to full)", overrides: ["missing_from_classifier_output"]`.
7. Wrap into a `PersonaScope` with `computed_at`, `cycle_index`, `classifier_model`, `head_sha`, and `files_summary` captured.
8. Write to `CardSession.persona_scope`.

## Latency contract

- Wall-clock budget: `classifier_latency_budget_seconds` (default 30 s; FR-016).
- Budget enforced via `asyncio.wait_for` around the `ConductingBackend.prompt(...)` call.
- Timeout → FR-006 fallback.

## Logging contract

| Event | Level | Fields |
|---|---|---|
| `persona_scope.classifier.start` | debug | `card_id, cycle_index, head_sha, file_count, persona_count, model` |
| `persona_scope.classifier.complete` | info | `card_id, cycle_index, latency_ms, depths={persona:depth}` |
| `persona_scope.classifier.forced_full` | info | `card_id, persona, path_class, matching_files` |
| `persona_scope.classifier.failed` | warning | `card_id, cycle_index, reason, fallback_to_full` |
| `persona_scope.classifier.unknown_persona_in_output` | debug | `card_id, unknown_persona` |
| `persona_scope.classifier.reusing_previous_cycle` | debug | `card_id, prior_cycle_index` (per FR-006 fallback to previous cycle) |

Logs MUST NOT include the rendered prompt body or the classifier's raw response (may contain project context excerpts).
