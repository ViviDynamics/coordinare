# Feature Specification: Diverse Multi-Backend QA Round

**Feature Branch**: `077-multi-backend-qa`
**Created**: 2026-05-29
**Status**: Draft
**Input**: User description: "Diverse multi-backend QA round for coordinare — exercise the full card lifecycle with a different agent backend per role, all driving the same limited self-hosted model (spark/qwen3.6:35b via the LiteLLM proxy), to surface per-backend lifecycle-correctness issues the way the single-backend claude_code round surfaced spec 076's Findings A/C and the resume-stage gap."

## Overview

Spec 076 (dispatcher dedup + lifecycle correctness) is merged to `main`. A
single-backend (claude_code) live round on `spark/qwen3.6:35b` validated those
fixes but isolated the model as the bottleneck — and, critically, exercised
only ONE backend. This round changes the variable under test from the *model*
to the *backend*: run the full card lifecycle with a **different agent backend
per role**, every backend driving the **same** model (`spark/qwen3.6:35b` via
the LiteLLM proxy), so any failure is attributable to backend integration, not
model capability.

Closing the round requires two new backend integrations and one enablement, all
confirmed as gaps against the current codebase:

- **Pi** (https://pi.dev) — no adapter exists; must be built (target role: closer).
- **opencode env_bootstrap** — backend exists but unproven for the bootstrap role and missing host creds.

The deliverable is both the working diverse-backend configuration AND a
per-backend findings report (which backends honor the role contracts on a small
model, which need fixes), analogous to spec 076's Phase 9 live-test findings.

## Clarifications

### Session 2026-05-29

- Q: How should the new Pi backend reach the shared spark/qwen3.6:35b model? → A: OpenAI-compatible via LiteLLM — the Pi backend is pointed at the LiteLLM proxy with a base-URL/provider override (like codex/hermes) and drives spark/qwen directly; it does NOT use Pi-hosted models.
- Q: What is the round's success bar — full target mapping or a diversity threshold? → A: Full mapping required — every role's mapped backend (including the new Pi backend and opencode env_bootstrap) must work for the round to be "done"; US4 (opencode) is blocking, not best-effort.
- Q: Given qwen's known limits, what counts as a stage "passing" for this backend round? → A: Backend-correctness, not card merge — a stage passes when its mapped backend runs, drives the shared model, and returns a valid terminal contract (DONE/PARTIAL_PROGRESS/BLOCKED). The card need not merge; model-quality failures (e.g., a weak plan) are recorded as findings, not round failures.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Each lifecycle role runs on its own backend, one shared model (Priority: P1)

An operator configures the `website` symphony so every lifecycle stage is
handled by a distinct agent backend, with all backends pointed at the single
shared `spark/qwen3.6:35b` model through the LiteLLM proxy. A card moves through
the entire lifecycle (assess → architect → implement → review → security → qa →
docs → close), each stage executed by its mapped backend, and the operator can
see from logs/dashboard which backend ran each stage.

**Why this priority**: This is the core capability under test — proof that
coordinare's per-role backend routing works end-to-end across heterogeneous
backends. It delivers value even with only the already-working backends
(junie, codex, claude_code, hermes), so it is an independently shippable MVP
that does not depend on the new Pi work.

**Independent Test**: With a config routing ≥4 distinct backends across the
lifecycle roles (all on `spark/qwen3.6:35b`), dispatch one card and confirm each
stage's performer container ran the backend mapped to that role, and the card
advances stage-to-stage without backend-routing errors.

**Acceptance Scenarios**:

1. **Given** a config mapping each role to a distinct backend on the shared model, **When** a card is dispatched through the lifecycle, **Then** each stage is handled by exactly the backend configured for that role (observable in performer container labels/logs).
2. **Given** two consecutive stages on different backends (e.g., codex implementer → claude_code reviewer), **When** the lifecycle advances between them, **Then** the handoff (branch, PR, relay feedback) is preserved across the backend switch with no loss of card state.
3. **Given** every backend is configured to use `spark/qwen3.6:35b` via LiteLLM, **When** any stage runs, **Then** that stage's LLM traffic targets the shared model (no backend silently falls back to a vendor-hosted model).

---

### User Story 2 - New "Pi" backend for the closer (Priority: P1)

An operator assigns the closer role to a new "Pi" backend. The closer stage
dispatches a Pi-backed performer that authenticates, drives `spark/qwen3.6:35b`
via LiteLLM, performs the closing review, and returns a terminal outcome the
orchestrator understands.

**Why this priority**: The closer in the target mapping has no working backend
without Pi. Pi is net-new integration work and the only way
to complete the intended diverse mapping; it is independently testable against
any single card stage.

**Independent Test**: Configure a performer endpoint with the Pi backend on a
throwaway card/role; confirm the Pi performer starts, reaches the model through
LiteLLM, and returns a recognized terminal status (DONE / PARTIAL_PROGRESS /
BLOCKED).

**Acceptance Scenarios**:

1. **Given** an endpoint configured for the Pi backend, **When** a stage is dispatched to it, **Then** the Pi performer authenticates and completes an LLM round-trip against `spark/qwen3.6:35b` via the proxy.
2. **Given** a Pi-backed closer finishes its review, **When** it returns, **Then** the orchestrator parses a valid terminal outcome (the same DONE/PARTIAL_PROGRESS/BLOCKED contract honored by existing backends) and advances or holds the card accordingly.
3. **Given** an invalid/unknown backend name in config, **When** the config is validated, **Then** the operator gets a clear error rather than a silent dispatch failure at runtime.

---

### User Story 3 — (withdrawn)

> A backend candidate evaluated for this round was found unable to drive the
> shared self-hosted `spark/qwen3.6:35b` model and was removed entirely. The US3
> slot is retained as a gap so US4–US6 and their task labels keep their numbers.

---

### User Story 4 - opencode handles env_bootstrap (Priority: P1)

An operator assigns the env_bootstrap role to the opencode backend; the
bootstrap performer installs the dev environment and runs the service-inference
probe, and a probe timeout does not fail the bootstrap (cache still becomes
ready).

**Why this priority**: env_bootstrap gates every card, so its backend must be
reliable; opencode is the user's chosen bootstrap backend and, per the clarified
full-mapping success bar, is a blocking deliverable for the round. It depends on
host credentials and validating the non-fatal-inference behavior under a second
backend, but is otherwise independent of US1–US3.

**Independent Test**: Configure env_bootstrap on opencode with credentials in
place; trigger a cold bootstrap and confirm the cache becomes ready and the card
proceeds, even if service-inference times out.

**Acceptance Scenarios**:

1. **Given** opencode is the env_bootstrap backend with valid credentials, **When** a cold bootstrap runs, **Then** the dev-environment install completes and the env cache is marked ready.
2. **Given** the service-inference probe times out under opencode, **When** the bootstrap finishes, **Then** the bootstrap is reported successful (non-fatal inference) and downstream stages dispatch.

---

### User Story 5 - Per-backend lifecycle-correctness findings (Priority: P3)

After the live diverse-backend run, the operator has a written per-backend
findings report: which backends honored their role contracts on the limited
model and which exhibited backend-specific failure modes (prompt-size/context
handling, terminal-contract parsing, idle/timeout behavior, commit/push
semantics), with concrete fixes filed.

**Why this priority**: The findings are the round's lasting output, but they are
produced by running US1–US4, so they come last.

**Independent Test**: Confirm a findings document exists enumerating each
backend used, its observed behavior per role, and any fixes/tasks raised.

**Acceptance Scenarios**:

1. **Given** the diverse-backend run has executed, **When** the operator reviews results, **Then** each backend has a documented verdict (contract-respecting vs. needs-fix) with evidence (logs/branch artifacts).

---

### User Story 6 - New "OpenClaw" backend for the reviewer (Priority: P1)

OpenClaw (https://openclaw.ai) — a self-hosted assistant from the same family
as Hermes/Pi — runs the **reviewer** role, replacing claude_code on that stage,
driving the shared `spark/qwen3.6:35b` model via the LiteLLM proxy. This adds a
seventh distinct backend to the round and exercises the reviewer's binary
JSON-only contract on a fresh backend. The reviewer only provides feedback —
humans approve/merge — so a new-backend reviewer cannot merge anything unsafe.

**Why this priority**: Another heterogeneous backend integration (same magnitude
as US2/US3/US4) that broadens the round's backend-coverage goal; added mid-round
on the active branch.

**Independent Test**: Configure `reviewer → openclaw` on an `openclaw-ephemeral`
endpoint and confirm a review stage runs on OpenClaw, drives `spark/qwen3.6:35b`
via LiteLLM (no vendor-hosted fallback), and returns a valid terminal outcome.

**Acceptance Scenarios**:

1. **Given** `reviewer` is mapped to `openclaw`, **When** a card reaches review, **Then** an OpenClaw container handles the stage and captured traffic shows `spark/qwen3.6:35b` via the proxy.
2. **Given** the limited model returns a review verdict, **When** OpenClaw finishes, **Then** the adapter reports a terminal outcome (done/error) parsed from `openclaw agent --json` (`meta.stopReason`).

---

### Edge Cases

- A backend silently falls back to a vendor-hosted model instead of the shared model (must be detectable, not silent).
- Two endpoints claim the same role — routing must be deterministic and surfaced.
- A backend returns output that does not match the terminal contract (DONE/PARTIAL_PROGRESS/BLOCKED) — must not be read as silent completion.
- A backend's credentials are missing/expired at dispatch time — must fail loudly with an actionable message, not hang.
- A handoff between two different backends mid-lifecycle loses branch/PR/relay state.
- A backend's required image is not built — must be caught at validation/startup, not mid-run.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST route each lifecycle role to its individually-configured backend within a single symphony, supporting multiple distinct backends concurrently across the lifecycle.
- **FR-002**: Every configured backend MUST be able to drive the shared `spark/qwen3.6:35b` model through the LiteLLM proxy; no backend may silently use a vendor-hosted model when a shared-model override is configured.
- **FR-003**: The system MUST provide a new "Pi" backend (https://pi.dev) that conforms to the existing performer backend protocol: authenticate, drive the configured model, and return a terminal outcome using the same DONE / PARTIAL_PROGRESS / BLOCKED contract as existing backends. The Pi backend MUST reach the model via an **OpenAI-compatible base-URL/provider override pointed at the LiteLLM proxy** (the same routing shape as codex/hermes) and MUST NOT use Pi-hosted models for this round.
- **FR-004**: The Pi backend MUST be selectable via configuration (a `pi` backend identifier) and runnable from a built performer image.
- **FR-005**: ~~(withdrawn)~~ — a backend-enablement requirement was dropped from the round (the candidate could not target a custom OpenAI-compatible endpoint). Number retained as a gap to keep FR-006…FR-012 stable.
- **FR-006**: The opencode backend MUST be usable for the env_bootstrap role, with documented credential setup; a service-inference probe timeout during opencode bootstrap MUST NOT fail the bootstrap (the env cache still becomes ready).
- **FR-007**: Configuration validation MUST reject an unknown/unimplemented backend name with a clear error before dispatch (no silent runtime failure).
- **FR-008**: Card state (branch, open PR, relay feedback, lifecycle position) MUST be preserved across a stage-to-stage handoff between two different backends.
- **FR-009**: The operator MUST be able to determine, from logs or dashboard, which backend executed each stage of a card.
- **FR-010**: The round MUST produce a per-backend findings report enumerating, for each backend exercised, whether it respected its role contract on the limited model and any backend-specific failure modes observed, with follow-up fixes recorded.
- **FR-011**: The diverse-backend configuration MUST be expressible in a single symphony config (the reconfigured `config.yaml`), with a documented backup of the prior config retained.
- **FR-013** *(added in-round — live finding)*: The implementer CI gate (075) MUST be scope-able **per persona independently of 074 persona-scope tiering**. The `persona_check_map` resolver MUST support a depth-agnostic `any` glob list that narrows the implementer's required checks when no 074 scope/depth is present, so operators can run a lighter gate for a weak-model implementer (e.g. exclude heavy `:js` feature tests, deferring them to reviewer/qa) without enabling the persona classifier.
- **FR-012**: The system MUST provide a new "OpenClaw" backend (https://openclaw.ai) that conforms to the existing performer backend protocol and reaches the model via an **OpenAI-compatible provider override pointed at the LiteLLM proxy** (writing `~/.openclaw/openclaw.json` with a custom `openai-completions` provider + model allowlist), MUST NOT use a vendor-hosted model for this round, MUST be selectable via an `openclaw` backend identifier and runnable from a built performer image, and MUST surface a terminal outcome parsed from `openclaw agent --json` (`meta.stopReason`).

### Key Entities *(include if feature involves data)*

- **Backend**: An agent CLI integration (junie, codex, claude_code, hermes, opencode, pi, openclaw) that executes a performer turn. Attributes: identifier, image, credential source, model/provider routing, terminal-contract handling.
- **Role → Backend mapping**: The per-lifecycle-stage assignment of a backend, within one symphony. Target round map: assessor→junie, architect/implementer/security→codex, reviewer→openclaw, qa→claude_code, tech_writer→hermes, env_bootstrap→opencode, closer→pi.
- **Per-backend finding**: A recorded observation about one backend's behavior in one role on the limited model — verdict (contract-respecting / needs-fix), evidence, and any fix/task raised.

## Success Criteria *(mandatory)*

### Measurable Outcomes

> **Stage-pass definition (per clarification):** a stage "passes" when its mapped
> backend runs, drives the shared `spark/qwen3.6:35b` model via LiteLLM, and
> returns a valid terminal contract (DONE / PARTIAL_PROGRESS / BLOCKED). The card
> need NOT merge; model-quality failures (e.g., a weak architect plan) are
> recorded as findings, not round failures.

- **SC-001**: A single card traverses every lifecycle stage with **each stage executed by its mapped backend and returning a valid terminal contract** (per the stage-pass definition above). The full target mapping is exercised — all backends in the mapping, **including the new Pi backend, OpenClaw, and opencode**, function (the round's success bar is the full mapping, not a subset).
- **SC-002**: **100% of stages** drive the shared `spark/qwen3.6:35b` model — zero stages silently use a vendor-hosted model (verifiable from captured LLM traffic).
- **SC-003**: The closer stage completes on the new Pi backend, returning a terminal outcome the orchestrator parses correctly (no unrecognized-output stalls).
- **SC-004**: ~~(withdrawn)~~ — was a backend-enablement success criterion for a candidate removed from the round. Number retained as a gap.
- **SC-005**: A cold env_bootstrap on opencode produces a ready env cache and the card proceeds, including when the service-inference probe times out.
- **SC-006**: Configuration validation flags an unknown backend name with an actionable error in under 1 validation pass (no runtime-only discovery).
- **SC-007**: A per-backend findings report exists covering every backend exercised, each with a verdict and supporting evidence.

## Assumptions

- The model is fixed at `spark/qwen3.6:35b` served via the existing LiteLLM proxy for the entire round; comparing models is explicitly out of scope.
- The dispatcher-dedup and lifecycle-correctness internals delivered by spec 076 (in-flight guard, reconciliation, idle/empty-output caps, resume-stage derivation, non-fatal service-inference) are in `main` and are NOT modified by this round.
- Only the `website` symphony is in scope; the validating card is a duplicate of the time-tracking card (#151, already in TODO).
- Host credentials for junie (`~/.junie`) and the LiteLLM proxy are already present; opencode is reached via an `OPENCODE_PROVIDER_*` config-write (no login mount needed).
- The round's success bar is the **full target mapping** (per clarification): every backend named in the mapping — junie, codex, claude_code, hermes, opencode, the new Pi, and the new OpenClaw — must function. A single backend may serve multiple roles (e.g., codex → architect + implementer + security); that is fine, but no mapped backend may be dropped or substituted for the round to be considered "done."

## Out of Scope

- Changing or comparing the underlying model (spark/qwen3.6:35b is fixed).
- Modifying the spec 076 dispatcher-dedup / lifecycle-correctness internals.
- Any symphony other than `website`.
- Persona/prompt changes (the milestone-based architect/implementer contracts landed with 076 are the committed defaults and are reused as-is).
