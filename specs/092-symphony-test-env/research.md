# Phase 0 Research: Symphony Test-Environment Injection

All open questions were resolved during brainstorming and captured in the authoritative
design doc
([2026-06-16-symphony-test-env-injection-design.md](../../docs/superpowers/specs/2026-06-16-symphony-test-env-injection-design.md)).
The spec carries **zero** `[NEEDS CLARIFICATION]` markers. This file records the decisions,
their rationale, and the alternatives rejected, so the design intent survives into tasks.

## Decision 1 — Injection mechanism: coordinare-owned parse-and-inject

**Decision**: Coordinare parses the dotenv file itself (a pure Python loader, no shell) and
merges the resulting `dict[str, str]` into each context's environment.

**Rationale**:
- Covers the in-process validator dry-run subprocess uniformly with the container contexts
  — a single seam, one parser, identical semantics everywhere.
- Lets coordinare redact cleanly: every value flows through the existing redacted `secrets`
  channel; logs emit only keys + source.
- Handles repo-relative and host-absolute sources behind one function with the same
  containment rule.

**Alternatives considered**:
- **Mount-and-`source`** the file inside the container — rejected: `source` is arbitrary
  code execution (a malicious or buggy file runs as shell), can't redact, doesn't reach the
  in-process validator subprocess, and would treat repo vs. host sources differently.
- **Agent-only discovery** (let the inference agent read and apply the env) — rejected:
  non-deterministic, no escape hatch for non-committed secrets, and no coordinare-side
  control over precedence vs. operational credentials.

## Decision 2 — Source model: exactly one of `repo_path` XOR `host_path`

**Decision**: `TestEnvConfig` accepts exactly one of `repo_path` (resolved inside the
cloned symphony repo, containment-checked) or `host_path` (absolute host path read
directly). Both-set or neither-set is a hard config-validation error at load time.

**Rationale**:
- `repo_path` serves the common case: a committed `.env.test` with non-secret test
  credentials, versioned with the project.
- `host_path` serves genuine secrets: a volume-mounted file outside the repo, never
  committed.
- Forbidding both-set avoids silent precedence ambiguity; forbidding neither-set avoids a
  meaningless empty block (omit it instead).
- Containment check on `repo_path` (reject `..` and absolute) prevents a config from
  reading arbitrary host files through the repo seam.

**Alternatives considered**: a single `path` field with mode auto-detection — rejected:
ambiguous (is `/abs/x` a host path or a repo path that happens to be absolute and should be
rejected?), and hides intent. Explicit XOR fields make the source obvious and the
validation rule trivial.

## Decision 3 — Parser semantics: dotenv-style, values taken literally

**Decision**: Split on the first `=`; skip blank lines and `#` comments; strip one layer of
matching surrounding quotes; ignore a leading `export `. No shell execution, no command
substitution, no inter-variable interpolation — `$(rm -rf /)` is returned as the literal
string `$(rm -rf /)`.

**Rationale**: Matches the de-facto dotenv convention developers already expect, while
eliminating the code-execution and surprising-expansion footguns of `source`. Literal
values keep the loader a pure, total function over file bytes.

**Edge cases pinned by tests**: quotes (single/double, one layer only), leading `export `,
`#` comments, blank lines, `=` inside the value (only first `=` splits), CRLF line endings,
literal `$(...)` returned verbatim.

**Alternatives considered**: full `python-dotenv` with interpolation — rejected as YAGNI
and as reintroducing expansion surprises; the design explicitly scopes interpolation out.

## Decision 4 — Fallback: agent emits path-only `test_env_source`

**Decision**: When no `test_env` block is configured, the inference agent emits a
repo-relative `test_env_source` path (never values) on `ServicesManifest`. Coordinare parses
that path through the *same* `load_test_env` (as if a `repo_path`) for the dry-run and
persists it with the env-cache so QA-runtime and performer contexts reload the same file.

**Rationale**: Zero-config symphonies still get their test env discovered deterministically
re-parsed by coordinare (not applied by the agent), preserving the secret invariant and the
single-parser guarantee. If neither config nor discovered path yields the needed var, the
existing `exit 75` gate fires — a genuine, actionable "missing", not a silent pass.

**Alternatives considered**: persisting the loaded values with the cache — rejected:
violates the invariant (values would be at rest in the snapshot) and would bake
symphony-specific secrets into a potentially-reused cache. Persist the **path** only; reload
at use time.

## Decision 5 — Precedence: operational secrets injected after test-env vars

**Decision**: Coordinare merges test-env vars first, then coordinare-owned operational
secrets (`GITHUB_TOKEN`, backend API keys) — so a test file can never clobber an
operational credential.

**Rationale**: Operational credentials are coordinare's to control; a project's test file is
untrusted input with respect to them. Last-writer-wins ordering enforces this without a
denylist.

## Decision 6 — Secret invariant & redaction (carried from spec 091, non-negotiable)

**Decision**: The manifest and logs carry only env-var **names** and file **paths**, never
literal values. Every value from `load_test_env` is treated as secret-like and routed
through the existing redacted `secrets` channel. Structured log events emit only the keys
(var names) and the source (`repo_path` / `host_path` / agent-discovered path).

**Rationale**: Direct continuation of the spec-091 `password_env_var` rule. The literal
secret lives only in the file and in-process environment.

## Best-practices notes (existing-codebase patterns to follow)

- **Config sub-model**: mirror `CloserPrChecksConfig` / `PersonaScopeConfig` in
  `config.py` — `model_config = ConfigDict(extra="forbid")`, truly-optional, cross-field
  rule via `@model_validator(mode="after")`. `SymphonyConfig.env_spec_files`'s existing
  absolute-path rejection is the template for `repo_path` containment.
- **Validator seam**: `validator.validate(..., env=...)` already merges
  `{**os.environ, **(env or {})}` in `_run()`; the main inference loop currently passes no
  `env`. Thread the loaded dict in there.
- **Payload seam**: `_build_regular_payload()` (and the env-bootstrap builder) in
  `http_performer_service.py` already carry a redacted `secrets` dict — merge test-env
  `KEY=VALUE`s into it, operational secrets after.
- **Manifest field**: add `test_env_source: str | None = None` to `ServicesManifest` in
  `schema.py`; surface it in `manifest_json_schema()` so the LLM can populate it; keep it
  path-only.

**Output**: All NEEDS CLARIFICATION resolved (none existed). Ready for Phase 1.
