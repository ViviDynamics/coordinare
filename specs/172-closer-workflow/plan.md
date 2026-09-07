# Implementation Plan: Closer Workflow with Thread Resolution Decided by Code

**Branch**: `172-closer-workflow` | **Date**: 2026-09-07 | **Spec**: [spec.md](spec.md)

## Summary

The closing_review stage becomes the eighth consumer of the spec-164 layer and the first whose common path makes no model call. Code fetches the PR's review threads, classifies each by pure rules, sends only the ambiguous ones to one schema-guarded call, checks every judgement against the thread's own comments, derives the verdict, posts one review, and resolves exactly the threads that earned it. The GraphQL fetch and the per-thread resolve are extracted from the existing `resolve_pr_review_threads` so the reviewer's behaviour is unchanged and both paths share one implementation.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing only. pydantic 2.x (thread, judgement and record models), the 164 layer (`Toolkit`, `budget`, `schema_guard`), the 169 reviewer's poster and write-free check, httpx through the performer's existing GitHub GraphQL helpers, structlog.
**Storage**: none; no coordinare state change.
**Testing**: pytest, both trees separately; every rule mutation-checked in the real tree; five fixtures stubbed in CI; live rounds through the gateway.
**Target Platform**: the performer container.
**Project Type**: single repository, performer tree plus config and docs.
**Performance Goals**: under 30 seconds with no model call, under two minutes at p90 with one (SC-004).
**Constraints**: default off; byte-for-byte prose path without the flag; no CI gating; nothing resolved on a failing verdict or after a failed post.
**Scale/Scope**: about 900 lines of performer code and tests; no coordinare code beyond `KNOWN_WORKFLOWS`.

## Constitution Check

| Principle | Check | Status |
| --- | --- | --- |
| I. Code Quality First | Classification and gate rules are pure functions with their own tests; the GraphQL fetch and resolve are extracted once and shared with the reviewer path rather than duplicated. No new dependencies. | PASS |
| II. Testing Discipline | Unit tests per rule with a named mutation, end-to-end scenarios against a fake GitHub, the main.py mapping including the prose path unchanged, five deterministic fixtures. | PASS |
| III. UX Consistency | One operator key, `workflow: closer`. The review body names what was resolved and what remains. Statuses are the ones coordinare already handles. | PASS |
| IV. Performance by Design | Budgets in the spec table; SC-002 and SC-004 measured live. | PASS |
| V. Clarity Before Action | research.md cites the code path each reuse rests on and the rule each classification encodes. | PASS |

## Project Structure

```text
specs/172-closer-workflow/{spec,plan,research,data-model,quickstart,tasks}.md, contracts/closing-record.schema.json, checklists/requirements.md

agent/performer/src/performer/github.py                 # extract fetch_review_threads() and resolve_review_threads(ids); resolve_pr_review_threads keeps its signature and uses them
agent/performer/src/performer/workflows/closer/
├── __init__.py      # CloserWorkflow.run: intake, classify, judge, gate, act, report
├── models.py        # Thread, ThreadComment, Classification, Judgement, ClosingRecord, model_judgements_schema
├── classify.py      # classify_thread and the rule predicates
├── personas.py      # JUDGE persona, render_threads
├── judge.py         # one schema-guarded call over the answered threads
├── gate.py          # quote_found, accept_judgement, verdict, apply
├── post.py          # build_closing_review, post through the reviewer's poster machinery
└── report.py        # ClosingRecord assembly
agent/performer/src/performer/workflows/{__init__,adapter,budget}.py   # register closer, toolkit, _STEP_BUDGETS["closing_judge"] = 4000
agent/performer/src/performer/main.py                   # closing_review branch on report["closing"], prose path untouched
src/coordinare/config.py                                 # KNOWN_WORKFLOWS += closer
src/coordinare/eval/closer_scenarios.py, tests/eval/closer_scenarios/
tests/unit/workflows/closer/, agent/performer/tests/unit/test_closer_report_path.py
config.example.yaml, docs/onboarding/04-harnesses-and-shims.md
```

**Structure Decision**: a sibling package under `workflows/`, as 165 to 171.

## Complexity Tracking

| Item | Why | Simpler alternative rejected because |
| --- | --- | --- |
| Extracting the GraphQL fetch and resolve from `resolve_pr_review_threads` | the closer needs every comment and per-thread resolution, the reviewer needs resolve-all; one implementation keeps them from drifting | a second copy in the workflow package would drift from the reviewer's, exactly the divergence spec 170's parity test exists to prevent |
| Posting the review before resolving | a failed post must not leave threads closed with no record of why | resolving first would leave the PR altered by a round that then held |

## Phases

Phase 0 research (this plan). Phase 1 design (data-model, contract, quickstart). Phase 2 implementation: the GitHub extraction and models first, then the core by hand. Phase 3 verification: both trees, mutation table, adversarial review, image rebuild, live rounds. Phase 4 rollout.
