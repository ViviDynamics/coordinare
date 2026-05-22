# Phase 0 Research: Hermes Performer Backend

**Feature**: 068-hermes-backend | **Date**: 2026-05-21

This document consolidates the technical decisions needed to remove all
`NEEDS CLARIFICATION` ambiguity from the Technical Context before Phase 1
design. Each entry follows the Decision / Rationale / Alternatives format.

---

## R-001: Hermes invocation path

- **Decision**: Use the non-interactive `hermes chat` CLI as an
  `asyncio.subprocess` for MVP, mirroring `agent/performer/src/performer/backends/junie.py`.
  Invoke as `hermes chat -q "<prompt>" --quiet -z --toolsets <allow_list>
  --provider <provider> --model <model>` with `HERMES_HOME` set to the
  job-scoped profile directory.
- **Rationale**: The CLI is the most stable contract surface across Hermes
  releases (`hermes-agent` PyPI ships it as the documented entrypoint), it
  matches the one-shot pattern this repo already validates (`claude_code`,
  `junie`), and it isolates Hermes' internal Python API from our process —
  upgrades to `hermes-agent` cannot regress our backend through import-time
  side effects. The CLI also accepts `--toolsets` directly, which is exactly
  the gate FR-006 demands.
- **Alternatives considered**:
  - *Direct `AIAgent` Python import* (`from run_agent import AIAgent`):
    Tighter coupling to Hermes' internal package layout; harder to gate
    capabilities at construction time; harder to enforce wall-clock timeout
    cleanly (would require an in-process cancel token rather than
    `proc.terminate()`).
  - *Hermes ACP (Agent Communication Protocol)*: Most future-proof but
    introduces a new transport surface the performer doesn't speak today.
    Deferred to a follow-up once the CLI adapter is shipped.

## R-002: Capability gating (FR-006)

- **Decision**: Pass an explicit `--toolsets` allow-list on every
  invocation: `terminal,file,search,browser,todo` (the coding-oriented set
  the spec mandates). Additionally write a job-scoped Hermes config file
  inside the temp `HERMES_HOME` that sets `disabled_toolsets:
  [gateway, messaging, cron, clarify, user_memory]` as a defense-in-depth
  guard in case future Hermes versions change CLI flag semantics.
- **Rationale**: `--toolsets` is the documented gate. The disabled-toolsets
  config is belt-and-suspenders: if a future Hermes release adds a new
  communication toolset to the default allow-list, our config still
  prevents it from being enabled. Listing the forbidden toolsets explicitly
  also gives an audit trail visible in logs.
- **Alternatives considered**:
  - *Disabled-toolsets only*: Relies on knowing every forbidden toolset
    name. New comms surfaces added upstream would slip through.
  - *Allow-list only*: Sufficient today but loses the explicit-deny audit
    signal that the spec's Key Entities section calls out.

## R-003: Profile isolation (FR-005, FR-011, SC-004)

- **Decision**: Create a job-scoped temp directory under
  `tempfile.mkdtemp(prefix="hermes-job-")` at `start()`, export it as
  `HERMES_HOME` for the subprocess, and `shutil.rmtree(..., ignore_errors=False)`
  it inside the adapter's terminal-state handler. Cleanup runs from a
  single `_finalize()` method invoked on every terminal path: normal
  completion, malformed-output error, subprocess crash, `stop()`, and
  `AGENT_TIMEOUT` expiry.
- **Rationale**: Two concurrent jobs cannot collide because each gets a
  fresh `mkdtemp` path. Unconditional cleanup matches the clarified intent
  (Q4: "failure-debugging context goes to structured logs, not retained
  temp directories"). Centralising the cleanup in `_finalize()` ensures
  every code path hits it.
- **Alternatives considered**:
  - *Symphony-scoped persistent profile* (`/devenv/hermes-profiles/<symphony>`):
    Deferred per spec Assumptions — adds shared-state risk this MVP
    explicitly excludes.
  - *Retain on failure*: Rejected by Q4 clarification.

## R-004: Relay feedback semantics (FR-002)

- **Decision**: Queue feedback strings on the adapter instance and
  concatenate them into the prompt of the *next* `hermes chat` invocation
  (one-shot replay). The CLI exits between turns; there is no live session
  to stream into.
- **Rationale**: Matches the `claude_code` / `junie` pattern this repo
  already exercises (and the memory note "Junie Is Its Own Harness" warns
  against persistent-session shoehorning). Confirmed by Q3 clarification.
- **Alternatives considered**:
  - *Mid-run stdin streaming into a long-running `hermes` process*:
    Hermes' `--quiet -z` non-interactive mode is exit-on-completion; a
    persistent stdin protocol would diverge from the documented entrypoint
    and re-introduce the very chat-UI surface FR-004 forbids.

## R-005: Status translation (FR-009)

- **Decision**: Map Hermes process states to the shared vocabulary:
  - subprocess running → `working`
  - subprocess exit 0 with non-empty parsable JSON output file → `done`
  - subprocess exit non-zero, missing output file, or empty/malformed
    output → `error`
  - `stop()` invoked → `error` with `reason="stopped"`
  - `AGENT_TIMEOUT` expiry → `error` with `reason="timeout"`
- **Rationale**: Mirrors `junie.py`'s status translation exactly so
  coordinare sees identical lifecycle behaviour (User Story 3, SC-002). FR-010
  requires malformed output to surface as terminal `error` — no retry, no
  loop.
- **Alternatives considered**: None — the spec's FR-009 / FR-010 / FR-010a
  fully constrain this surface.

## R-006: Timeout enforcement (FR-010a)

- **Decision**: Rely on the existing `settings.AGENT_TIMEOUT` enforcement
  in `agent/performer/src/performer/main.py` (lines ~1014-1025 and
  ~2035-2036). On expiry, coordinare's session loop already calls
  `backend.stop()`; the Hermes adapter's `stop()` will `proc.terminate()`,
  await with a short grace period, then `proc.kill()` if needed, then
  `_finalize()` to clean up the profile dir.
- **Rationale**: Q1 clarification explicitly chose reuse over a new
  Hermes-specific timer. Keeps the adapter free of duplicate timing
  machinery and inherits future tuning of `AGENT_TIMEOUT` for free.
- **Alternatives considered**:
  - *Adapter-internal timer*: Rejected by Q1.

## R-007: Env var contract (FR-012)

- **Decision**: The adapter reads the following env vars (with sane
  defaults where Hermes provides them) and forwards them to the
  subprocess; categories follow Hermes upstream conventions, exact names
  may be updated as Hermes evolves:
  - `HERMES_PROVIDER` — provider id (e.g., `openai`, `anthropic`,
    `custom`)
  - `HERMES_BASE_URL` — provider base URL (for self-hosted endpoints)
  - `HERMES_API_KEY` — provider credential
  - `HERMES_MODEL` — model identifier
  - `HERMES_HOME` — **always** overridden by the adapter to the job-scoped
    temp dir; an operator-set value is intentionally ignored to satisfy
    FR-005.
- **Rationale**: Listing categories rather than fixing names matches spec
  Assumption ("env-var names follow Hermes' own current conventions").
  Overriding `HERMES_HOME` unconditionally enforces FR-005.
- **Alternatives considered**:
  - *Honour operator `HERMES_HOME`*: Directly violates FR-005.

## R-008: Prompt shape (FR-003)

- **Decision**: Reuse the existing `Score`-to-prompt helpers used by
  `opencode.py` / `codex.py` / `junie.py`. Specifically, build the prompt
  in the same order: persona instructions → card title → card description
  → acceptance criteria → clarifications → relay feedback (concatenated
  queue from R-004) → architecture-plan reference → role-specific output
  requirements.
- **Rationale**: FR-003 mandates parity. Reusing the shared helpers means
  a future change to the prompt skeleton propagates to Hermes without
  duplication.
- **Alternatives considered**:
  - *Hermes-specific prompt template*: Would drift from peer backends and
    violate FR-003.

---

## Open items handed to Phase 1

None. All Technical Context unknowns resolved; data-model.md and
contracts/ can proceed.
