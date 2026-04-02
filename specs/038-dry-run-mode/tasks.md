# Tasks: Dry-Run Mode (038)

- [x] T001: Create `DryRunGitHubService` implementing `GitHubServiceProtocol` with recorded actions
- [x] T002: Create `DryRunAgentService` implementing `AgentServiceProtocol` with synthetic responses
- [x] T003: Create `DryRunResult` pydantic BaseModel with planned_actions, lifecycle_stages, assessment_result, board_transitions
- [x] T004: Implement `execute_dry_run(card_id, config)` orchestrator that builds lifecycle preview
- [x] T005: Add `dry-run` subcommand to argparse in `__main__.py` with `--card` and `--config` args
- [x] T006: Add `_cmd_dry_run` handler that loads config, calls `execute_dry_run`, prints result
- [x] T007: Add `POST /api/dry-run/{card_id}` endpoint to `create_dashboard_app` in `dashboard.py`
- [x] T008: Unit tests for `DryRunGitHubService` (all protocol methods record actions)
- [x] T009: Unit tests for `DryRunAgentService` (dispatch, check_status, check_health, relay_feedback)
- [x] T010: Unit tests for `DryRunResult` model construction and serialization
- [x] T011: Unit tests for `execute_dry_run` with default and multi-role configs
- [x] T012: Unit tests for dashboard `/api/dry-run/{card_id}` endpoint (success + missing config)
