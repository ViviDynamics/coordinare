# Tasks: Reasoning policy and truncation classification

## Setup and foundation
- [x] T001 Refresh design decisions and policy transport contract in specs/163-reasoning-policy/plan.md and specs/contracts/dispatch-payload.md; preserve requests for absent policies and use the existing token-limit terminal outcome for truncation.
- [x] T002 Run .claude/commands/speckit.analyze.md over the completed artifacts before implementation.

## US1 — classify output exhaustion for every role
Independent check: structured length beats malformed prose for every role; normal empty responses retain their existing routing.
- [x] T003 [US1] Add structured precedence and prose fallback tests in tests/unit/test_163_truncation_classification.py (FR-001–006).
- [x] T004 [US1] Extend src/coordinare/services/assessor_failure.py to accept structured finish reason and prioritize truncation (FR-002–006).
- [x] T005 [US1] Route truncation through the existing output-budget block in src/coordinare/graph/nodes/monitor_performer.py; preserve scoped retry derivations in that file and src/coordinare/graph/nodes/handle_system_error.py (FR-001,007,008).
- [x] T006 [US1] Test all lifecycle roles, no unchanged truncation retry, and unchanged non-assessor ENV_BLOCKED/malformed routing in tests/unit/test_163_derivation_scope.py (FR-001,004,007,008).

## US2 — optional policy reaches requests
Independent check: a declared policy reaches actual outbound proxy requests; absent policy keeps request bytes unchanged.
- [x] T007 [US2] Add an optional validated disable_thinking policy to ModelEndpoint in src/coordinare/config.py and carry it through dispatch to performer Score and proxy launch (FR-009–014).
- [x] T008 [US2] Apply the policy after canonical wire translation in agent/performer/src/performer/proxy/upstreams.py; ensure direct backend and workflow CLI dispatches with a policy launch DualModelProxy and reject incompatible native endpoints (FR-010,011,014).
- [x] T009 [US2] Add model validation, dispatch contract, and real HTTP forwarding tests in tests/unit/test_163_reasoning_policy.py and agent/performer/tests/unit/proxy/ (FR-009–014).

## US3 — preserve per-model evidence
Independent check: known harmful GLM opt-in is rejected and no shipped model is opted in.
- [x] T010 [US3] Record beneficial/harmful/unmeasured policy evidence and reject harmful opt-ins in src/coordinare/config.py and specs/163-reasoning-policy/policy-evidence.json, retaining research.md evidence (FR-015–017).
- [x] T011 [US3] Pin harmful GLM, beneficial Qwen, unknown model, and default-off cases in tests/unit/test_163_policy_safety.py (FR-015–017).

## Validation and operator documentation
- [x] T012 Update specs/163-reasoning-policy/quickstart.md with a tracked configuration example with the final supported policy surface.
- [x] T013 Run make lint, make test-all, performer tests, full CI and independent review; record results and any measured overhead in specs/163-reasoning-policy/plan.md.

## Dependencies and execution
T001 → T002 → US1 → US2 → US3 → final validation. Tests within each story precede implementation where practical. Classification tests and policy evidence tests can be prepared independently after the design check; actual implementation proceeds serially in this worktree. Both feature halves ship together in the issue PR.
