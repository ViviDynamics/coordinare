# Feature Specification: Living Project Wiki via the Documenter

**Feature Branch**: `124-openwiki-documenter`
**Created**: 2026-07-03 · **Re-scoped**: 2026-07-04
**Status**: Draft
**Input**: User direction: "Evolve our own tech_writer to behave like OpenWiki is meant to be used — maintain a living, structured project wiki that documents the project as a record of truth other agents read. Keep a symphony-init wiki prerequisite. Use our own reliable performer (self-hosted models); do not adopt OpenWiki itself."

## Overview

Coordinare's documenter (`tech_writer`) role today edits documentation alongside a code PR and is skipped when a PR touches no `docs/` paths. This feature **repurposes the documenter to maintain a living project wiki** — a structured, always-current, source-grounded set of markdown pages under `docs/wiki/` (a `README.md` entrypoint plus focused section pages), together with concise pointers appended to the repo's `AGENTS.md`/`CLAUDE.md` so every other performer (architect, implementer, QA) can read the record of truth while planning and executing.

The documenter keeps using coordinare's **existing, reliable performer path** — the `hermes` backend on `spark/gpt-oss:120b` via the LiteLLM gateway, emitting the JSON `{files:[{path,content}]}` contract that the documenting stage already commits. The *behavior* changes (a rewritten persona that maintains a structured wiki), not the engine.

Coordinare also gains a **symphony-init wiki prerequisite**: when a brand-new symphony starts against a repository that has no wiki, coordinare builds the seed wiki (a one-shot documenter run) before dispatching any non-documentation work, so the record of truth exists from the first card onward.

### Why not OpenWiki (the original plan)

The original 124 plan adopted **OpenWiki** (LangChain's repo-documentation CLI) as a new backend. A live proof-of-concept against a real repo (`ViviDynamics/website`) proved it **unreliable on our stack** and it is **dropped**:

- OpenWiki is built on the DeepAgents tool-calling harness. Against every self-hosted model on our LiteLLM gateway it failed a large fraction of runs with assorted tool-schema / message-format errors — `qwen3.6:35b` 0/1 (`invalid message format`), `glm-4.7-flash` 0/1 (malformed tool call **+ it roamed the host filesystem**), `gpt-oss:120b` ~3/5 (`write_todos` / `invalid message format`). Cloud models (which are **not permitted** for this) fared no better (`gpt-5-nano` 1/3, `gpt-4o-mini` 1/2). The failures were prompt-independent — richer prompts introduced *new* failure modes.
- OpenWiki is v0.0.1 (pre-release); its DeepAgents ↔ gateway compatibility is immature, and it commits its own output in ways that conflict with coordinare's dispatch/PR model.

Our own documenter (hermes + gpt-oss:120b, JSON contract) is the same model that reliably serves the current tech_writer — so it maintains the wiki without the DeepAgents tool-calling fragility. We keep OpenWiki's *intent* (a living, source-grounded, navigable wiki + agent pointers, built once and refreshed incrementally), not its implementation.

## Clarifications

### Session 2026-07-03 (original OpenWiki plan — superseded by the 2026-07-04 pivot)

- Q: How do ongoing wiki updates land relative to each card's work? → A: In-card — the documenting stage stays a pre-merge pipeline hook and commits wiki changes into each card's own pull request.
- Q: What gates the auto-merge of the symphony-init seed-wiki PR? → A: Required CI status checks green AND the coordinare bot-reviewer's approval.

### Session 2026-07-04 (pivot)

- Q: Adopt OpenWiki, or evolve our own documenter? → A: **Drop OpenWiki** (v0.0.1 DeepAgents tool-calling too flaky against self-hosted models; cloud models not permitted). Evolve the existing `tech_writer` (hermes/gpt-oss:120b, JSON `{files}` contract) to produce OpenWiki-style living documentation.
- Q: Where does the wiki live? → A: `docs/wiki/` (reuse the existing convention; the website repo already uses it).
- Q: Keep the symphony-init wiki gate? → A: **Yes**, powered by our own documenter (a one-shot init run), not OpenWiki.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The documenter maintains a living project wiki (Priority: P1)

As a coordinare operator, I want the documenter to maintain a structured, source-grounded wiki under `docs/wiki/` (a README entrypoint plus section pages) and keep `AGENTS.md`/`CLAUDE.md` pointing at it, so the project has an always-current record of truth that other performers read — without me writing docs by hand.

**Why this priority**: This is the core capability and it reuses the reliable existing engine. It delivers value on its own: every card's documenting stage now produces/refreshes a navigable wiki instead of ad-hoc doc edits.

**Independent Test**: Dispatch a documenter job against a repo with recent changes and confirm the result commits structured `docs/wiki/` pages (README + at least one section) grounded in real source, updates the `AGENTS.md`/`CLAUDE.md` OpenWiki-style reference section, and reports the standard `{files}` / `docs_committed` result.

**Acceptance Scenarios**:

1. **Given** a repo whose `docs/wiki/` is stale relative to merged code, **When** the documenter runs, **Then** it refreshes the affected wiki pages (grounded in current source, not invented) and updates the `AGENTS.md`/`CLAUDE.md` pointer section, committing via the normal `{files}` contract.
2. **Given** the documenter runs, **Then** `docs/wiki/README.md` exists as the entrypoint with a repository overview and links to every section page.
3. **Given** the documenter has nothing to change, **When** it runs, **Then** it reports success with an empty change set and commits nothing.
4. **Given** the documenter runs, **Then** it uses the existing hermes/`spark/gpt-oss:120b` performer path (no new backend, no cloud model).

---

### User Story 2 - Symphony-init wiki prerequisite (Priority: P2)

As a coordinare operator starting a brand-new symphony, I want coordinare to build the seed wiki before any real work is dispatched, so the record of truth exists from the first card, without me merging a docs PR by hand.

**Why this priority**: Delivers "record of truth from day one." Depends on US1 (the documenter behavior) and reuses the env-bootstrap gate pattern.

**Independent Test**: Start a symphony whose repo has no `docs/wiki/`; confirm coordinare dispatches a one-shot init documenter run, opens a seed-wiki PR that auto-merges once CI is green and the bot-reviewer approves, holds non-documentation dispatch until the wiki exists, and — on restart with the wiki present — does not re-initialize.

**Acceptance Scenarios**:

1. **Given** a new symphony with no `docs/wiki/`, **When** coordinare starts, **Then** it dispatches the documenter in init mode and holds non-documentation dispatch until the wiki is present.
2. **Given** the init run opens a seed-wiki PR, **When** its required CI checks pass and the bot-reviewer approves, **Then** coordinare auto-merges it without a human merge.
3. **Given** the repo already has `docs/wiki/`, **When** coordinare starts, **Then** it marks the wiki initialized and proceeds to normal work.
4. **Given** init repeatedly fails, **When** the attempt budget is exhausted, **Then** coordinare holds the symphony and emits one operator notification (no silent deadlock).
5. **Given** coordinare restarts with the wiki-initialized marker set, **When** it resumes, **Then** it does not re-initialize.

---

### User Story 3 - Grounded, consistent documentation (Priority: P3)

As a consumer of the wiki (human or agent), I want every page grounded in real source/git evidence and organized consistently, so I can trust it and navigate it.

**Why this priority**: Quality is what makes the wiki a usable record of truth; it hardens US1.

**Independent Test**: Review generated pages and confirm claims cite real files/modules, `README.md` links to all sections, there are no invented APIs/behaviors, and one concept has one canonical home (no duplicated content across pages).

**Acceptance Scenarios**:

1. **Given** a generated page, **Then** its substantive claims reference real source files, existing docs, or git history — nothing invented.
2. **Given** the wiki, **Then** it avoids thin stub pages: a section page has real explanatory value or its content is folded into a broader page.

---

### Edge Cases

- **Pre-existing docs**: the repo already has a `docs/wiki/` (e.g. the website). The documenter refreshes/extends it in place rather than duplicating; init detects it as already-initialized.
- **Instruction-file conflict**: the `AGENTS.md`/`CLAUDE.md` OpenWiki-style reference section is added/updated without corrupting other coordinare-owned or human content in those files.
- **Auto-merge blocked**: branch protection or a required human review prevents auto-merging the seed-wiki PR → coordinare holds + notifies (no silent deadlock).
- **Restart mid-initialization**: the persisted marker + attempt budget resume without double-initializing.
- **No-op run**: the documenter finds nothing to change → empty `{files}`, `docs_committed`, no commit.
- **Cardless init dispatch**: the seed-wiki run is not tied to a card; it needs its own branch + PR (see the wiki-init-gate contract).

## Requirements *(mandatory)*

### Functional Requirements

**Documenter behavior (the living wiki)**

- **FR-001**: The `tech_writer` documenter MUST maintain a structured project wiki under `docs/wiki/` — a `README.md` entrypoint plus focused section pages (e.g. architecture, domains, data models, deployment, integrations, testing) as the repository warrants.
- **FR-002**: The documenter MUST ground every substantive claim in real source files, existing docs, or git history, and MUST NOT invent files, modules, APIs, or behavior.
- **FR-003**: The documenter MUST add or refresh a concise OpenWiki-style reference section in the repo's top-level `AGENTS.md` and `CLAUDE.md` pointing at `docs/wiki/README.md`, without corrupting surrounding content.
- **FR-004**: The documenter MUST support two modes: an initial full build and an incremental update scoped to what changed since the last documented point.
- **FR-005**: The documenter MUST use coordinare's existing performer path (the `hermes` backend on `spark/gpt-oss:120b` via LiteLLM) and MUST emit the existing documenter result contract — JSON `{files:[{path,content}]}` → `files_modified`, terminal status `docs_committed`/`error` — so downstream orchestration is unchanged. No new backend and no cloud model are introduced.
- **FR-006**: The documenter's responsibility MUST change from editing docs only when a PR touches `docs/` paths to keeping the wiki current based on the card's code changes; the prior docs-path skip MUST be removed for the wiki-maintaining documenter.
- **FR-007**: `docs/wiki/README.md` MUST be the entrypoint, with a repository overview and links to every section page. Pages MUST avoid thin stubs (fold trivial content into a broader page) and keep one canonical home per concept.

**Symphony-init wiki gate**

- **FR-008**: When a symphony starts against a repository with no `docs/wiki/`, the system MUST build the wiki (a one-shot init documenter run) as a prerequisite before dispatching any non-documentation work.
- **FR-009**: The seed wiki MUST land via a pull request; for the initialization bootstrap only, the system MUST auto-merge it once required CI status checks pass AND the coordinare bot-reviewer approves — no human merge required.
- **FR-010**: The system MUST persist a durable "wiki initialized" marker (surviving restart) and MUST NOT re-initialize when a wiki already exists.
- **FR-011**: Wiki initialization MUST be bounded by an attempt budget with a circuit breaker (default 3); on exhaustion the system MUST hold the symphony and emit one operator notification rather than deadlocking silently or retrying forever.
- **FR-012**: The init gate MUST be feature-flagged (default off) and MUST only hold dispatch when its init trigger is wired, so it can never hold a symphony with nothing to satisfy it.

**Observability**

- **FR-013**: The system MUST emit observability records for wiki initialization (starting, complete, skipped-already-initialized, failed) and for documenter runs, to the job's existing capture mechanism.

### Key Entities

- **Project Wiki**: the structured markdown artifact under `docs/wiki/` (README entrypoint + section pages) plus the `AGENTS.md`/`CLAUDE.md` pointer section and a durable "last documented" marker — the symphony's living record of truth.
- **Documenter (tech_writer)**: the existing hermes/gpt-oss:120b performer, driven by a rewritten persona to build/maintain the wiki; emits the unchanged `{files}`/`docs_committed` contract.
- **Wiki Initialization State**: durable, per-symphony gate state — an initialized marker, attempt count, and exhausted/circuit-breaker flag — that gates first work and survives restart.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In 100% of cold-start runs against a repository with no `docs/wiki/`, a populated wiki is available to all performers before the first non-documentation card is dispatched (or the symphony is explicitly held with an operator notification).
- **SC-002**: The documenter reliably completes on the existing self-hosted performer path — no dependence on a cloud model and no new backend (SC verified by the documenter using hermes/`spark/gpt-oss:120b`).
- **SC-003**: `docs/wiki/README.md` exists as an entrypoint linking to all section pages, and `AGENTS.md`/`CLAUDE.md` carry the pointer section, after an init run.
- **SC-004**: Generated pages are source-grounded — a human spot-check finds no invented APIs/behaviors and pages cite real files.
- **SC-005**: No generated documentation reaches the default branch without either human review (ongoing updates, in the card's PR) or the required CI checks passing and the bot-reviewer approving (initialization bootstrap).
- **SC-006**: The wiki-init gate never deadlocks silently: whenever initialization cannot complete within the attempt budget, the symphony is held and an operator notification is emitted.
- **SC-007**: Enabling the documenter's wiki behavior introduces no change to downstream orchestration — it reports the same result contract consumed by the rest of the pipeline.

## Assumptions

- **Wiki home** is `docs/wiki/` (reuse the existing convention). A repo with an existing `docs/wiki/` is refreshed in place.
- **Engine** is the current documenter: `hermes` backend, `spark/gpt-oss:120b`, JSON `{files}` contract — reliable on self-hosted models (unlike OpenWiki's DeepAgents loop).
- **The wiki structure follows OpenWiki's best-practice shape** (README entrypoint + section pages, source-grounded, one canonical home, AGENTS/CLAUDE pointers) — encoded in the documenter persona.
- **Init-bootstrap auto-merge** uses coordinare's existing CI-green + bot-review gate; branch protection permitting.
- **Cloud models are not permitted** for the documenter.

## Dependencies

- The existing `hermes` documenter performer + the `{files}` documenting contract (`main.py` documenting terminal path) — the reliable engine, reused.
- The environment-bootstrap gate pattern — mirrored for the restart-safe, circuit-broken wiki-init gate.
- Coordinare's PR / review-gate / auto-merge machinery — to land wiki changes and auto-merge the seed bootstrap.
- The notification and job-capture (observability) mechanisms — for gate events and the operator notification.

## Out of Scope

- Adopting OpenWiki / any new backend, and any DeepAgents-based tooling (dropped — see Overview).
- Any cloud model for the documenter.
- Serving the wiki as a rendered website (committed markdown only).
- Changing the documentation engine for any role other than the documenter.
