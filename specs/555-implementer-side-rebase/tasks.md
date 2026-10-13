# Tasks: Implementer-Side Rebase

- [x] T001 Add `ImplementerRebaseConfig` and `ProjectConfiguration.implementer_rebase` (default on, owner decision); document in `config.example.yaml`
- [x] T002 Add `RebaseOutcome.DELEGATED`; drop the ephemeral clone paths from `RebaseJob`
- [x] T003 Remove clone, rebase, push and conflict-extraction code from `services/rebase.py`
- [x] T004 Add `build_divergence_feedback`, `request_implementer_rebase`, `implementer_rebase_enabled`
- [x] T005 Pre-dispatch guard: classify, request or proceed, fail open (US1, US2, US3)
- [x] T006 `check_board`: one read-only divergence sweep replaces the edge round and the proactive rebase
- [x] T007 `merge_pr` and the multi-session preflight record main and rebase nothing (US4)
- [x] T008 Unit tests: `tests/unit/test_555_implementer_side_rebase.py`; rewrite the guard, sweep and lifecycle tests; update 134, 549, 551, 552, merge_pr, check_board
- [x] T009 Superseding notes on specs 047, 096, 097; event mapping in `contracts/events.md`
- [x] T010 Audit other daemon-side code-touching paths into `quickstart.md` and the PR (FR-012)
- [x] T012 Feedback entry carries `kind`, `main_sha`, `expected_head_sha`; body asks for a rebase
- [x] T013 Performer: `_rebase_request`, `_rebase_onto_base` at workspace setup, `_push_requested_rebase` under a lease (FR-014)
- [x] T014 Real-git performer tests: `agent/performer/tests/unit/test_555_requested_rebase.py`
- [x] T015 Anti-thrash hold escalates to blocked with a reason (FR-013)
- [x] T016 No request while a documenter side run is active (FR-015)
- [x] T017 Replace `git ls-remote` with `GitHubService.get_branch_head_sha`; remove `_run_git`, `_git_env_for_token`, `repo_url_from_config`
- [x] T011 `make lint`, `make typecheck`, `make test-all`, `config validate` on `config.example.yaml`
