# Tasks: Per-Role Model Selection

**Input**: Design documents from `/specs/037-per-role-model-selection/`

## Phase 1: Setup

- [x] T001 Add `model: str | None = None` field to PerformerRoleConfig in src/coordinare/config.py

## Phase 2: US1 — Backend Override via Dispatch Payload (P1)

- [x] T002 Include `backend` and `model` from role config in dispatch payload in src/coordinare/graph/nodes/dispatch_performer.py
- [x] T003 Read `backend` and `model` from dispatch payload in performer main.py, override AGENT_BACKEND
- [x] T004 Add `*, model: str | None = None` to BackendAdapter.start() protocol in agent/performer/src/performer/backends/base.py
- [x] T005 Update claude_code.py start() to accept and use model parameter
- [x] T006 Update opencode.py start() to accept model parameter
- [x] T007 Update codex.py start() to accept model parameter
- [x] T008 Add test: dispatch payload includes backend from role config in tests/unit/graph/nodes/test_dispatch_performer.py
- [x] T009 Add test: dispatch payload includes model when set in tests/unit/graph/nodes/test_dispatch_performer.py

## Phase 3: Polish

- [x] T010 Run full test suite and lint check
- [x] T011 Mark tasks complete
