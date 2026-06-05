# Implementation Plan: Diverse Multi-Backend QA Round

**Branch**: `077-multi-backend-qa` | **Date**: 2026-05-29 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/077-multi-backend-qa/spec.md`

## Summary

Run the full card lifecycle with a different agent backend per role, every
backend driving the single shared model `spark/qwen3.6:35b` via the LiteLLM
proxy, so the variable under test is the BACKEND, not the model. Per the
clarified success bar (full target mapping required), this means delivering two
new backend capabilities and one enablement, then validating the whole mapping
on a live card:

- **Pi backend (new)** — implement `PiBackend` (`agent/performer/src/performer/backends/pi.py`) to the existing `BackendAdapter` protocol, reaching the model via an OpenAI-compatible base-URL/provider override to LiteLLM (the codex/hermes routing shape, per clarification — NOT Pi-hosted models). Register it in the backend factory. Target role: closer.
- **OpenClaw backend (new)** — implement `OpenClawBackend` (`agent/performer/src/performer/backends/openclaw.py`) to the `BackendAdapter` protocol, reaching the model via an OpenAI-compatible provider config (`~/.openclaw/openclaw.json` from `OPENCLAW_PROVIDER_*`) to LiteLLM. Installed via the entrypoint (npm). Target role: reviewer.
- **opencode env_bootstrap** — wire env_bootstrap to opencode, document `~/.opencode` creds, and confirm the dev-env install + (non-fatal) service-inference probe behave under opencode.

Technical approach: extend the performer's backend layer (factory registration + two adapters/overrides) and the diverse-routing config (already drafted in `config.yaml`), then a live run produces a per-backend findings report. A stage "passes" on backend-correctness (its backend runs, drives the shared model, returns a valid terminal contract), not on the card merging.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod on 3.14.5 via uv)
**Primary Dependencies**: pydantic 2.x (config + schemas), the performer backend layer (`agent/performer/src/performer/backends/`), httpx (HTTP performer transport), docker CLI (ephemeral performers), the LiteLLM proxy (OpenAI-compatible model gateway)
**Storage**: N/A for this round (no new persisted state; reuses 076's snapshot/state_store unchanged)
**Testing**: pytest (`.venv/bin/pytest`), with backend unit tests (`agent/performer/tests/unit/`) + coordinare config/contract tests (`tests/unit`, `tests/contract`); coverage gate `--cov-fail-under=90`; lint `.venv/bin/ruff check`
**Target Platform**: single-host coordinare daemon orchestrating ephemeral Docker performer containers (`coordinare-performer:full`; pi and openclaw install via the entrypoint on start)
**Project Type**: single project — coordinare daemon (`src/coordinare/`) + performer package (`agent/performer/`)
**Performance Goals**: N/A — the round is correctness/coverage-focused; the shared model is intentionally slow (qwen) and not under test
**Constraints**: model fixed at `spark/qwen3.6:35b` via LiteLLM; spec 076 dispatcher/lifecycle internals MUST NOT be modified; `website` symphony only; backend routing must be observable per stage
**Scale/Scope**: 1 symphony, 1 validating card (#151), 7 role-backends in the mapping; ~2 new backend adapters (Pi, OpenClaw) + config + tests + findings report

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. The new `PiBackend` and `OpenClawBackend` reuse the established `BackendAdapter` protocol and the one-shot CLI + provider-config pattern (single-responsibility adapters, no new abstractions). Per the project's interface-first guidance, Pi/OpenClaw sit *beneath* the existing backend facade; no facade changes. Lint-clean required.
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS (planned). New code carries unit tests (Pi adapter: start/status/terminal-contract parsing; OpenClaw adapter: provider-config emission + `openclaw agent --json` terminal parsing; backend factory: `pi`/`openclaw` resolve, unknown name raises `UnsupportedBackendError`). Coverage MUST NOT drop below 90%. Contract tests assert the provider-routing env contracts using the producers' real values (lesson from 076 T170). Tests deterministic — hermetic env (clear ambient provider vars, per 076's claude-proxy-env fix).
- **III. User Experience Consistency** — PASS. "User" = the operator: per-stage backend visibility (FR-009) reuses existing dashboard/log surfaces; an unknown/misconfigured backend fails loudly with an actionable message (FR-007), not a silent hang.
- **IV. Performance by Design** — PASS (N/A). No hot-path or latency work; the round is coverage-focused and explicitly excludes model/perf comparison.
- **V. Clarity Before Action** — PASS. All five clarification taxonomy gaps were resolved in `/speckit.clarify` (Pi routing, success bar, stage-pass definition) and recorded in spec `## Clarifications`; no `NEEDS CLARIFICATION` markers remain in spec or this plan.

**Result: PASS** — no violations; Complexity Tracking not required.

## Project Structure

### Documentation (this feature)

```text
specs/077-multi-backend-qa/
├── plan.md              # This file
├── research.md          # Phase 0 output (below)
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output (backend provider-routing contracts)
├── checklists/
│   └── requirements.md  # /speckit.specify quality checklist (passed)
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

```text
agent/performer/
├── src/performer/backends/
│   ├── __init__.py        # backend factory — register "pi" → PiBackend, "openclaw" → OpenClawBackend
│   ├── base.py            # BackendAdapter protocol (unchanged contract Pi/OpenClaw implement)
│   ├── pi.py              # NEW: PiBackend adapter (OpenAI-compat via LiteLLM)
│   ├── openclaw.py        # NEW: OpenClawBackend adapter (OpenAI-compat via LiteLLM)
│   ├── codex.py           # reference pattern (config.toml provider override)
│   └── opencode.py        # reused for env_bootstrap (+ OPENCODE_PROVIDER_* config-write)
├── Dockerfile.full        # reused for codex/claude/hermes/junie/opencode; entrypoint installs pi/openclaw
├── entrypoint.sh          # ADD: pi) and openclaw) npm install arms
└── tests/unit/
    ├── test_pi_backend.py            # NEW
    ├── backends/test_openclaw_backend.py   # NEW
    └── test_backends_factory.py      # EXTEND: pi/openclaw resolve, unknown raises

src/coordinare/
├── config.yaml            # diverse-backend mapping (pi/opencode/openclaw wired)
└── (config validation)    # ensure unknown backend surfaces early (FR-007)
```

**Structure Decision**: Single project. All new code lives in the performer
backend layer (`agent/performer/src/performer/backends/`) plus entrypoint install
arms; coordinare-side changes are limited to config wiring and (if needed)
surfacing an unknown-backend error earlier. No new top-level modules, no
schema/state changes — this round is backend-coverage, not lifecycle-internals.

## Phase 0 — Research Outputs

See [research.md](./research.md). Key decisions:
- Pi adapter modeled on `CodexBackend` (CLI + OpenAI-compatible provider override via `PI_PROVIDER_BASE_URL` → LiteLLM); registered in the `get_backend` factory.
- OpenClaw adapter modeled on the Hermes/Pi one-shot pattern (CLI + OpenAI-compatible provider config via `OPENCLAW_PROVIDER_*` → LiteLLM, `openclaw agent --local --json`); registered in the factory; installed via the entrypoint.
- opencode reused unchanged for env_bootstrap; round validates non-fatal service-inference (076 T175 already in `main`) under opencode + documents `~/.opencode` creds.
- FR-007 (unknown backend) is enforced by `UnsupportedBackendError` in the factory; research confirms whether coordinare surfaces it pre-dispatch or only in-container, and closes any gap.

## Phase 1 — Design & Contracts

- [data-model.md](./data-model.md) — Backend registration entry, provider-routing env contract (per backend), role→backend mapping, per-backend finding record.
- [contracts/](./contracts/) — `backend-provider-routing.md`: the env-var contract each backend honors to reach LiteLLM + the shared model (codex/junie existing; Pi/OpenClaw new), and the `BackendAdapter` terminal-contract expectations.
- [quickstart.md](./quickstart.md) — operator steps to build images, set creds, launch the diverse round on #151, and read per-stage backend attribution.

### Agent context update

Run `.specify/scripts/bash/update-agent-context.sh claude` to record the new
backends (`pi`, `openclaw`) in the agent context file.

## Re-Evaluation of Constitution Check (post-design)

Still **PASS**. The design adds two adapters/overrides beneath the existing
`BackendAdapter` facade and one Dockerfile — no new abstractions, no facade
bloat, no schema changes. Testing discipline upheld (unit + contract tests,
≥90% coverage, hermetic env). No complexity deviations to track.

## Complexity Tracking

No constitution violations — table intentionally omitted.
