# Tasks — PR conversation feedback

## Foundations
- [x] T001 Read issue, reviewer authorization, REST update semantics, and existing feedback delivery seams; record specs/550-pr-conversation-feedback/research.md.
- [x] T002 Write spec/plan/data-model/contracts and run speckit.analyze before product implementation.

## US1 — Human request
- [x] T003 [US1] Add failing API and actionable/noise/approval/authorization and operator activity provenance tests in tests/unit/test_550_pr_conversation_feedback.py.
- [x] T004 [US1] Implement dedicated fetch/classification helper and operator activity emission in src/coordinare/services/pr_conversation_feedback.py and services/github.py.
- [x] T005 [US1] Integrate independent conversation requests into graph/nodes/monitor_pr.py without altering submitted-review supersession or merge authority.

## US2 — Edits and polling
- [x] T006 [US2] Add failing edit/revert/unchanged-body and multiple-same-author tests in tests/unit/test_550_pr_conversation_feedback.py.
- [x] T007 [US2] Implement version tracking and safe update-prefix progression in services/pr_conversation_feedback.py.

## US3 — Recovery
- [x] T008 [US3] Add failing snapshot/hydration/retirement, failed fetch and controlled budget tests in tests/unit/test_550_pr_conversation_feedback.py.
- [x] T009 [US3] Wire schema30 through graph/state.py, session.py, state_store.py, daemon.py and snapshot JSON contract/version assertions.
- [x] T010 [US3] Verify real performer payload and no repeated completed request through existing durable feedback lifecycle.

## Verification and shipping
- [x] T011 Update operator interaction docs and specs/550-pr-conversation-feedback/quickstart.md with request/edit/restart behavior.
- [ ] T012 Run adversarial review, full preflight, Copilot review, required CI and squash merge; record ship result.
- [ ] T013 Rerun live interaction scenarios after all six follow-ups ship; record evidence in docs/superpowers/specs.

## Dependencies
T003→T004→T005; T006→T007; T008→T009→T010. Integration depends on #548/#549/#551/#552. Root owns implementation and shipping; isolated agents handle separate lifecycle issues.
