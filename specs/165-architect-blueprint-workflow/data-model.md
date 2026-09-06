# Data Model: Architect Role Workflow with a Blueprint Hand-off

## Blueprint (performer output, persisted on the session)

| Field | Type | Bounds | Notes |
| --- | --- | --- | --- |
| `summary` | str | <= 600 chars | one paragraph, what and why |
| `milestones` | list[Milestone] | 1 to 7 | ordered; each independently implementable |
| `modules` | list[Module] | 0 to 12 | directory or layer plus a one-line note |
| `data_model` | DataModel | | `changes` list 0 to 12 |
| `interfaces` | list[Interface] | 0 to 12 | name, kind (endpoint, class, event, cli), one-line contract |
| `risks` | list[str] | 0 to 8, <= 300 chars each | |
| `criteria` | list[Criterion] | 1 to 12 | the verification brief |
| `docs` | list[DocTopic] | 0 to 8 | the documentation brief; empty means no documenter |
| `size` | "small" \| "large" | set by code, never by the model | see Size |

**Milestone**: `goal` (<= 200), `scope` (list[str] <= 8 paths or modules), `done_when` (<= 300).
**Module**: `path` (<= 120), `note` (<= 200).
**DataModel**: `changes` list of `{kind: table|model|column|index|migration, name, note}`; `none` is an empty list.
**Interface**: `name`, `kind`, `contract` (<= 300).
**Criterion**: `surface` (<= 120: a route, screen, command or API), `action` (<= 200), `expected` (<= 300), `kind: functional|visual|command`.
**DocTopic**: `topic` (<= 120), `location` (<= 160: wiki path or section), `say` (<= 400: what the reader must learn).

Validation: pydantic models with `extra="forbid"`, `min_length`/`max_length` on every list and string; the JSON Schema shown to the model is rendered from the same models (164 `schema_guard.render_schema`). A blueprint with zero milestones or zero criteria is invalid, not hollow-but-accepted.

## Briefs (derived at dispatch, never stored)

| Brief | Reader | Fields |
| --- | --- | --- |
| `implementation_brief` | implementer | `summary`, `milestones`, `modules`, `data_model`, `interfaces`, `risks`, `size` |
| `documentation_brief` | documenter (side run) | `summary`, `docs`, `modules` |
| `verification_brief` | qa | `criteria`, `summary` |

Disjointness rule: `docs` never reaches the implementer or QA; `milestones` never reaches the documenter or QA; `criteria` reach QA only. A contract test asserts the three projections against a full blueprint.

`implementer_single_turn: bool` accompanies the implementation brief when `size == "small"`.

## Size (pure function)

`small` when `len(milestones) <= 1 and len(data_model.changes) == 0 and len(interfaces) == 0`; otherwise `large`. Tested at the boundaries (1 milestone with one column change is large; 1 milestone with nothing else is small).

## Survey budget and executed checks

`SurveyBudget`: `max_commands` (default 12), `max_output_chars` (default 4000). `SurveyRecord`: per command `{command, allowed: bool, refusal_reason, exit_code, truncated}`. `WriteFreeCheck`: an executed check recorded in the report (`git status --porcelain` empty at the end of the run, plus the count of refused commands), the same shape as 164's `ExecutedCheck`.

## Coordinare persistence (schema 17)

`PersistedSession.blueprint: dict | None` (the validated blueprint plus `size` and `created_at`, `blueprint_hash`).
`PersistedSession.documenting_side: DocumentingSideRun | None` with `status: pending|running|done|failed`, `dispatched_at`, `blueprint_hash`, `session_id`, `head_sha`, `result_reason`.

Rules: a new blueprint replaces the old one and resets `documenting_side`; a side run is dispatched at most once per `blueprint_hash`; a failed side run is recorded and never retried within the card (the end-of-lifecycle documenting pass covers it).

## Card context additions (dispatch payload)

`implementation_brief`, `documentation_brief`, `verification_brief` (each `dict | absent`), `implementer_single_turn` (`bool | absent`). Absent keys stay off the payload so the pre-165 path is unchanged in size and shape. Declared on `Score` and registered in `specs/contracts/dispatch-payload.md`.
