# Tasks: Implementer-Side Rebase

- [x] T001 Add `ImplementerRebaseConfig` and `ProjectConfiguration.implementer_rebase` (default off); document in `config.example.yaml`
- [x] T002 Add `RebaseOutcome.DELEGATED`; drop the ephemeral clone paths from `RebaseJob`
- [x] T003 Remove clone, rebase, push and conflict-extraction code from `services/rebase.py`
- [x] T004 Add `build_divergence_feedback`, `request_implementer_rebase`, `implementer_rebase_enabled`
- [x] T005 Pre-dispatch guard: classify, request or proceed, fail open (US1, US2, US3)
- [x] T006 `check_board`: one read-only divergence sweep replaces the edge round and the proactive rebase
- [x] T007 `merge_pr` and the multi-session preflight record main and rebase nothing (US4)
- [x] T008 Unit tests: `tests/unit/test_555_implementer_side_rebase.py`; rewrite the guard, sweep and lifecycle tests; update 134, 549, 551, 552, merge_pr, check_board
- [x] T009 Superseding notes on specs 047, 096, 097; event mapping in `contracts/events.md`
- [x] T010 Audit other daemon-side code-touching paths into `quickstart.md` and the PR (FR-012)
- [x] T011 `make lint`, `make typecheck`, `make test-all`, `config validate` on `config.example.yaml`
