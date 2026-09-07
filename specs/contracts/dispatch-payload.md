# Contract: Dispatch Payload (Coordinare → Performer)

**Last updated**: 2026-04-06
**Boundary**: `AgentService.dispatch_card()` → wire (subprocess stdin) → `Score(**payload)` in performer

## Overview

The dispatch payload is the primary data contract between the coordinare and performer.
It flows through `AgentService.dispatch_card()`, is serialized as JSON over the subprocess
transport, and deserialized into the performer's `Score` pydantic model.

**Critical rule**: `AgentService.dispatch_card()` MUST pass through ALL `card_context` fields.
It MUST NOT selectively filter fields. The `Score` model on the performer side is the
authoritative schema — any field not on `Score` is silently dropped by pydantic (`extra="ignore"`).

## Field Registry

### Card Identity (set by check_board)

| Field | Type | Required | Set by | Used by |
|-------|------|----------|--------|---------|
| `id` | str | yes | check_board | performer logging |
| `title` | str | yes | check_board | backend prompt, PR title |
| `description` | str | yes | check_board | backend prompt |
| `acceptance_criteria` | list[str] | no | check_board | backend prompt |
| `status` | str | no | check_board | informational |
| `previous_status` | str | no | check_board | informational |
| `issue_id` | str | no | check_board | issue details lookup |
| `issue_number` | int | no | check_board | informational |
| `issue_url` | str | no | check_board | informational |

### Workspace (set by WorkspaceManager, overlaid by AgentService)

| Field | Type | Required | Set by | Used by |
|-------|------|----------|--------|---------|
| `repo_url` | str | yes | WorkspaceManager | git clone/push, Score validation |
| `branch` | str | yes | WorkspaceManager | git checkout, push, PR head |
| `github_token` | str | yes* | WorkspaceManager | git auth, GitHub API calls |
| `workspace_path` | str | no | WorkspaceManager | informational (not used by performer) |

*Empty string allowed when K8s transport injects auth via secrets.

### Performer Lifecycle (set by dispatch_performer)

| Field | Type | Required | Set by | Used by |
|-------|------|----------|--------|---------|
| `role` | str | yes | dispatch_performer | role-specific status handling (architect, reviewer, security, QA, assessor, etc.) |
| `persona_instructions` | str | no | dispatch_performer | backend prompt — role-specific behavior |
| `workflow` | str | no | dispatch_performer (from role config, spec 164) | performer selects a role workflow from `performer.workflows` registry; absent/None means the pre-164 single-backend path. MUST be declared on `Score` — `extra="ignore"` would otherwise drop it and the workflow would silently never run |
| `workflow_env` | dict[str,str] | no | dispatch_performer (from role config `workflow_env`, spec 164) | role workflow boot env, layered over the env cache's activation env: `QA_APP_START_COMMAND`, `QA_APP_SEED_COMMAND`, `PORT`. The operator channel for an app start command `infer_app_start_command` cannot infer. MUST be declared on `Score` |
| `qa_findings` | list[dict] | no | dispatch_performer (from prior QA `WorkflowResult`, spec 164) | implementer backend prompt — structured repair brief from the previous QA round. Shape mirrors `scanner_findings`: `file`/`line`/`category`/`severity` plus `criterion`, `plan_check_id`, `expected`, `observed`, `evidence`, `repro_command`. Reports only; never prescribes a fix |
| `implementation_brief` | dict | no | dispatch_performer (projection of `PersistedSession.blueprint`, spec 165) | implementer backend prompt via `_card_docs`: summary, milestones, modules, data_model, interfaces, risks, size |
| `documentation_brief` | dict | no | dispatch_performer (projection of the blueprint, spec 165) | documenter side-run prompt: summary, docs, modules |
| `verification_brief` | dict | no | dispatch_performer (projection of the blueprint, spec 165) | QA workflow plan step: criteria, summary |
| `implementer_single_turn` | bool | no | dispatch_performer (from `blueprint.size == "small"`, spec 165) | implementer persona: no milestone loop, no PARTIAL_PROGRESS |
| `assessment` | dict or absent | no | dispatch_performer (from `PersistedSession.assessment`, spec 166) | architecting workflow: structured product reading (goal, expected behaviour, out-of-scope items, questions, assumptions, criteria with their source, carried clarifications). Injected ONLY into architecting stage dispatch; absent from all other stages. When present, architect intake renders it as the first section. |
| `review_findings` | dict or absent | no | dispatch_performer (from `PersistedSession.review_findings`, spec 169) | implementing workflow: structured findings from the reviewer (changed_files with hunks, findings with anchors and categories, survey commands, dispositions, coverage pass outcome, verdict, GitHub post result). Injected ONLY into implementing stage dispatch when a prior reviewing stage reported changes_requested with findings; absent from reviewing and all other stages. When present, implementer plan selects the repair lane and groups findings by file. |
| `relay_feedback` | list[dict] | no | dispatch_performer | backend prompt — human review comments to address. 126: entries MAY carry `id` (str, `fb-N`), `raiser` (str stage name or `ci`) and `re_raised` (bool) — the feedback-ledger contract keys the implementer echoes back via `feedback_dispositions` |
| `disputed_feedback` | list[dict] | no | dispatch_performer | 126: `{id, body, reason}` items the implementer disputed, injected ONLY into the raising stage's dispatch so its verdict adjudicates them |
| `pr_url` | str | no | dispatch_performer (from card) | reviewer/security post reviews to PR |
| `pr_node_id` | str | no | dispatch_performer (from card) | terminal status response for coordinare |
| `pr_diff` | str | no | dispatch_performer (fetched via get_pr_diff) | backend prompt — raw unified diff for review roles (reviewer/closer/qa/tech_writer) so the model has the changes inline; omitted on fetch failure |
| `architecture_plan_path` | str | no | dispatch_performer (from card) | backend prompt — reference architect's plan |
| `clarifications` | list[dict] | no | assess_card (embedded in card); dispatch_performer for assessing stage (spec 166, from `PersistedSession.card_clarifications`) | backend prompt — Q&A history; injected into assessing dispatch only from prior clarification rounds |
| `prior_clarifications` | list[dict] | no | dispatch_performer (from `PersistedSession.assessor_open_questions`, feature 123) | assessor intake (spec 166 `build_intake`, merged with `clarifications`): Q&A answers from prior assessor runs; injected on re-dispatch only, absent on first dispatch; each entry is `{"question": str, "answer": str}`. MUST be declared on `Score`, because `extra="ignore"` would otherwise drop it and the assessor would re-ask every answered question while the dispatch looked correct |

### Environment Cache (set by dispatch_performer, feature 060)

> **Transport note**: These fields are delivered via `JobInitPayload` over HTTP to containerized (Docker) performers. Subprocess performers receive them through the `Score` model pathway above. The `metadata` dict is a free-form extension point; performer images MUST declare any keys they read as explicit fields on their job-init model to avoid silent drops.

| Field | Type | Required | Set by | Used by |
|-------|------|----------|--------|---------|
| `metadata` | dict[str, str] | no | dispatch_performer | extension point for per-job context |
| `metadata.env_cache_path` | str (within metadata) | no | dispatch_performer (when env cache ready) | performer activation — path to symphony's env cache subdirectory inside the container (e.g. `/devenv/my-project-a1b2c3`); absent when no cache configured or cache dir not yet ready |
| `coordinare_manages_services` | bool | no | env_cache (BootstrapJobPayload, feature 116) | env_bootstrap performer — when False (default, sourced from `EnvCacheConfig.coordinare_manages_services`), the performer owns env setup end-to-end and SKIPS the 101 service-readiness gate (success = toolchain verify.sh). Declared on the `Score` model (extra="ignore" would otherwise drop it). Defaults True so an older/synthetic payload preserves the coordinare-managed path |

### Backend Selection (set by dispatch_performer, feature 037)

| Field | Type | Required | Set by | Used by |
|-------|------|----------|--------|---------|
| `backend` | str | no | dispatch_performer | select AI backend (opencode, junie, claude_code, codex) |
| `model` | str | no | dispatch_performer | select model within backend |

### GitHub Enterprise (set by dispatch_performer, feature 036)

| Field | Type | Required | Set by | Used by |
|-------|------|----------|--------|---------|
| `github_api_url` | str | no | dispatch_performer | override GitHub REST API base URL |

## Enforcement Points

### 1. AgentService.dispatch_card() — `src/coordinare/services/agent_service.py`

MUST pass through `dict(card_context)` without field filtering.
Workspace fields from `WorkspaceInfo` are overlaid on top.

### 2. Score model — `agent/performer/src/performer/models.py`

MUST have explicit fields for every payload key the performer needs to read.
Uses `model_config = {"extra": "ignore"}` — unknown fields are silently dropped.
When adding a new field to the dispatch payload, it MUST also be added to Score.

### 3. Backend prompt builders

Each backend (`opencode.py`, `claude_code.py`, `codex.py`) has a `_build_task_prompt(score)`
function that constructs the prompt sent to the AI. It MUST include:
- `score.persona_instructions` (role-specific behavior)
- `score.relay_feedback` (human review comments)
- `score.acceptance_criteria`
- `score.clarifications` (Q&A history)
- `score.architecture_plan_path` (when present)

### 4. Contract tests — `tests/contract/test_dispatch_payload.py`

Integration tests that verify the full pipeline:
`card_context` → `AgentService.dispatch_card()` → wire → `Score(**payload)`

These tests MUST assert that every field in this registry survives the journey.

## How to use

### Field Registry format

The `## Field Registry` section contains one or more markdown tables. Each table row registers a payload field that crosses the coordinare → performer boundary. The first column of each table is the **field name** (wrapped in backticks in the raw table source, e.g. `` `field_name` ``).

`speckit.analyze` reads this registry automatically when it runs. If a spec or plan adds a new field to the dispatch payload but does not register it here, `speckit.analyze` will emit a `[CONTRACT MISMATCH]` warning.

### Adding a new field

1. Add a row to the appropriate section in `## Field Registry`:
   ```
   | `new_field` | str | no | your_node | what it's used for |
   ```
2. Follow the **Change Protocol** below to propagate the field through the codebase.
3. Run `speckit.analyze` after updating the spec and plan to confirm no mismatch warnings remain.

### How speckit.analyze uses this file

During the **Contract Check** detection pass, `speckit.analyze`:
1. Reads all `specs/contracts/*.md` files.
2. Parses every `## Field Registry` section's markdown tables to extract field names (first column values).
3. Scans the feature's `spec.md` and `plan.md` for field names mentioned in payload-change context.
4. Emits `[CONTRACT MISMATCH] Field '{field}' referenced in spec/plan but not registered in {contract_file}` for each unregistered field.

The check is **skipped silently** when no contract files exist or when the spec/plan contains no payload-change language.

## Change Protocol

When adding a new field to the dispatch payload:

1. Add the field to this contract document
2. Add the field to `card_context` in `dispatch_performer.py`
3. Add the field to `Score` in `agent/performer/src/performer/models.py`
4. Add the field to the relevant backend `_build_task_prompt()` if the AI needs it
5. Add a contract test assertion in `tests/contract/test_dispatch_payload.py`
6. Run contract tests to verify end-to-end delivery
