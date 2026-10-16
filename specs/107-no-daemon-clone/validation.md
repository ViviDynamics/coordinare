# Validation for issue 107

All changes remain uncommitted on branch `107-work`.

## Design

The daemon sends a versioned scan request using the changed paths from GitHub.
The prose security performer scans its own checkout before backend startup,
keeps the result in runtime session state, supplies findings to the backend and
returns them in `report.security_floor`. The daemon validates the current report
and applies the existing severity and routing gate. An explicitly configured
security workflow supplies its own scan record. Only development subprocess
transport can use a daemon checkout.

## Acceptance and evidence

| Acceptance | Evidence | Result |
| --- | --- | --- |
| Non subprocess preparation never runs git or creates a clone directory | `test_non_subprocess_prepare_never_runs_git`, 16 combinations of transport, clone hint and new or existing card | exit 0 |
| Subprocess checkout stays available for development | `test_subprocess_performer_always_needs_the_daemon_clone`, `test_subprocess_dispatch_keeps_the_local_clone`, workspace regression tests | exit 0 |
| Performer runs the scan and returns authoritative findings | `test_security_job_scans_in_own_clone_and_returns_report`, including repeated terminal polls, and existing scanner normalization fixtures | exit 0 |
| Missing or invalid scan evidence holds closed across versions | `test_missing_old_or_malformed_scan_holds_even_with_stale_clean_state`, `test_old_performer_ignoring_workflow_config_cannot_pass`, `test_invalid_or_old_dispatch_cannot_report_clean_scan` | exit 0 |
| Same severity and routing gate applies | `test_reported_high_finding_overrides_pass_and_routes_to_implementer`, `test_scanner_replaces_a_model_downgrade_of_the_same_finding`, existing architect and halt routing tests | exit 0 |
| Request survives transport and typed parsing | `test_scan_request_survives_http_metadata_and_score_validation`, `test_prose_security_scan_request_survives` | exit 0 |
| Workflow reports cannot bypass prose scans | `test_prose_response_cannot_bypass_floor_by_claiming_workflow_report` and existing configured workflow regressions | exit 0 |
| Tool failures and unsafe targets hold closed | `test_scanner_failure_json_is_never_a_clean_scan`, `test_scan_paths_cannot_escape_workspace_through_symlink`, `test_scan_paths_starting_with_dash_are_operands` | exit 0 |

## Final selections

* Daemon, transport, contract and workflow tests: 765 passed, exit 0.
* Performer security, dispatch and provenance tests: 170 passed, exit 0.
* Node client checks and dashboard decomposition tests: 11 passed, exit 0.
* Ruff on every touched Python file: exit 0.
* Strict mypy: 275 source files, exit 0.
* Ruff baseline ratchet: exit 0, no baseline changes.
* Diff whitespace check: exit 0.

The first regression run failed before implementation: the daemon selection
reported 14 failures and the performer selection reported 2 failures. Further
scanner JSON and version compatibility regressions also failed before fixes.

A broader performer selection had 251 passes and 17 failures, exit 1. All 17
failures were PermissionError from the sandbox denying macOS process enumeration
through psutil. No tests were skipped or suppressed for this change. The full
suite, real scanner execution and browser tests are left for the maintainer.

The final daemon selection covered:

```
tests/unit/test_107*
tests/unit/graph/nodes/test_dispatch_performer.py
tests/unit/graph/nodes/test_monitor_performer.py
tests/unit/graph/nodes/test_security_workflow_gating.py
tests/unit/services/test_security_scanner.py
tests/unit/test_workspace.py
tests/unit/test_mypy_allowlist.py
tests/contract/test_security_payload.py
tests/contract/test_dispatch_payload.py
tests/unit/services/test_http_performer_service.py
tests/unit/workflows/security/
```

The final performer selection covered:

```
agent/performer/tests/unit/test_107_security_floor.py
agent/performer/tests/unit/test_security_report_path.py
agent/performer/tests/unit/test_044_feedback_provenance.py
agent/performer/tests/unit/test_main.py::TestSecurityPerformer
agent/performer/tests/unit/test_main.py::TestHandleDispatch
```

The Node and dashboard selection covered:

```
tests/unit/test_138_client_js.py
tests/unit/test_436_dashboard_decomposition.py
```

## Changed files

* `AGENTS.md` (modified)
* `agent/performer/Dockerfile.full` (modified)
* `agent/performer/src/performer/main.py` (modified)
* `agent/performer/src/performer/models.py` (modified)
* `agent/performer/src/performer/security_floor.py` (added)
* `agent/performer/src/performer/security_scanner.py` (added)
* `agent/performer/src/performer/status_paths.py` (modified)
* `agent/performer/tests/unit/test_044_feedback_provenance.py` (modified)
* `agent/performer/tests/unit/test_107_security_floor.py` (added)
* `agent/performer/tests/unit/test_main.py` (modified)
* `agent/performer/tests/unit/test_security_report_path.py` (modified)
* `config.example.yaml` (modified)
* `docs/onboarding/04-harnesses-and-shims.md` (modified)
* `pyproject.toml` (modified)
* `specs/107-no-daemon-clone/plan.md` (modified)
* `specs/107-no-daemon-clone/pr-body.md` (added)
* `specs/107-no-daemon-clone/spec.md` (modified)
* `specs/107-no-daemon-clone/tasks.md` (modified)
* `specs/107-no-daemon-clone/validation.md` (added)
* `specs/contracts/dispatch-payload.md` (modified)
* `src/coordinare/graph/nodes/dispatch_performer.py` (modified)
* `src/coordinare/graph/nodes/monitor/body.py` (modified)
* `src/coordinare/services/security_floor.py` (added)
* `src/coordinare/services/security_scanner.py` (removed)
* `src/coordinare/workspace.py` (modified)
* `tests/contract/test_security_payload.py` (modified)
* `tests/unit/graph/nodes/test_dispatch_performer.py` (modified)
* `tests/unit/graph/nodes/test_monitor_performer.py` (modified)
* `tests/unit/graph/nodes/test_security_workflow_gating.py` (modified)
* `tests/unit/services/test_security_scanner.py` (modified)
* `tests/unit/test_107_no_daemon_clone.py` (modified)
* `tests/unit/test_107_security_floor.py` (added)
* `tests/unit/test_mypy_allowlist.py` (modified)
