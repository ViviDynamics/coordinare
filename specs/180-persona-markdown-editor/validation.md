# Validation for 180

All changes are local to the working tree. No commit, push, daemon launch or deployment was performed.

## Design and analysis

The spec, plan and tasks were analyzed against the constitution before implementation. No blocking conflicts were found. The implementation uses a dependency free escaped Markdown subset, a shared selector and source editor, existing Config review and dirty tracking, and the shared atomic YAML writer. Inactive editors stay mounted to retain drafts. An untouched source baseline preserves CRLF strings despite textarea normalization. The persona write service rechecks the file hash after parsing and before calling the shared writer.

## Executed checks

| Check | Result | Exit |
| --- | --- | --- |
| Ruff on all five changed Python files | Passed | 0 |
| mypy, package coordinare | 275 source files passed | 0 |
| scripts/ruff_baseline.py | Baseline holds, 1409 violations across 43 rules | 0 |
| Focused regression selection below | 370 passed, four existing structlog warnings | 0 |
| tests/unit/test_dashboard.py with persona selection | 9 passed | 0 |
| Node syntax checks for personas.js, config.js, app.js and persona_180_checks.js | Passed | 0 |
| Shipped source Node execution through test_shipped_editor_node_checks | Passed | 0 |
| Browser collection for Chromium and Firefox | 16 cases collected | 0 |
| git diff check and added text policy checks | Passed | 0 |

The regression selection was:

```sh
python -m pytest \
  tests/unit/test_180_persona_editor.py \
  tests/unit/test_persona_service.py \
  tests/unit/test_130_persona_history.py \
  tests/unit/test_143_settings.py \
  tests/unit/test_143_review.py \
  tests/unit/test_143_review2.py \
  tests/unit/test_143_review3.py \
  tests/unit/test_156_config_ui_concurrency.py \
  tests/unit/test_157_require_config_hash.py \
  tests/unit/test_158_guard_remaining_config_writes.py \
  tests/unit/test_config_write_service.py \
  tests/unit/test_221_symphony_editor.py \
  tests/unit/test_221_symphony_editor_js.py \
  tests/unit/test_176_dashboard_app_script.py \
  tests/unit/test_349_static_js.py \
  tests/unit/test_436_dashboard_decomposition.py -q --tb=short
```

An earlier broader run including all of test_dashboard.py had 165 passes and two failures, exit 1. The failures were test_check_port_available_exits_on_conflict and test_check_port_available_succeeds_on_free_port: the sandbox rejects their localhost binds with PermissionError. No tests were changed or disabled for that restriction.

## Acceptance coverage

| Item | Executed coverage | Browser coverage ready for operator execution |
| --- | --- | --- |
| One selected persona in Config and Personas | Node selector visibility, selection, URL and draft checks, exit 0 | test_one_persona_keyboard_tools_and_responsive_preview; test_read_only_page_selection_survives_reload_and_links_to_editor |
| Read only Personas with editor links | Existing 143 contract test, exit 0 | test_read_only_page_selection_survives_reload_and_links_to_editor |
| Source at least 60 percent of viewport | CSS uses 60vh; physical bounds unmeasured | test_one_persona_keyboard_tools_and_responsive_preview at 1280 by 800 |
| Highlighted source and rendered preview | Shipped renderer and highlighter Node checks, exit 0 | test_one_persona_keyboard_tools_and_responsive_preview |
| Dirty marker, navigation guard, reset confirmation | Node dirty tracking and existing Config guard regressions, exit 0 | test_exact_reviewed_source_reset_and_unsaved_guard; test_conflict_reapply_keeps_other_persona_drafts; test_reset_preserves_another_personas_draft |
| Exact instruction strings | test_untouched_api_text_round_trips_exactly and Node source baseline checks, exit 0 | test_untouched_source_does_not_write_normalized_text |
| Script injection cannot execute | Node hostile markup and unsafe protocol checks, exit 0 | test_preview_cannot_execute_persona_markup verifies DOM and execution state |
| One reusable component | Shipped component and Config snapshot Node checks; hashed asset tests, exit 0 | Config uses personaEditor and personaMount; Personas shares personaSelector and personaMarkdown |
| Keyboard, both themes, 375px | Node text transformations, exit 0; visual and focus behavior unverified | test_one_persona_keyboard_tools_and_responsive_preview; read only test at 375px |

Browser execution and the full suite are left to the operator as requested. Run:

```sh
python -m pytest tests/e2e/test_180_persona_editor.py -m e2e --browser chromium --browser firefox
```

## Changed files

1. src/coordinare/dashboard/dashboard_static/personas.js
2. src/coordinare/dashboard/dashboard_static/config.js
3. src/coordinare/dashboard/dashboard_static/app.js
4. src/coordinare/dashboard/dashboard_static/shell.css
5. src/coordinare/dashboard/assets.py
6. src/coordinare/dashboard/routers/personas.py
7. src/coordinare/services/persona_service.py
8. tests/unit/test_180_persona_editor.py
9. tests/js/persona_180_checks.js
10. tests/e2e/test_180_persona_editor.py
11. docs/operators/dashboard-config-editing.md
12. specs/180-persona-markdown-editor/spec.md
13. specs/180-persona-markdown-editor/plan.md
14. specs/180-persona-markdown-editor/tasks.md
15. specs/180-persona-markdown-editor/validation.md

## Deployment

Redeploy the daemon and reload the dashboard, using a hard refresh if an older stylesheet remains cached. No schema migration, config change or external dependency is needed. Existing persona endpoint contracts, version history and reset semantics remain. The writer continues to remove YAML comments while preserving instruction string contents. Prompt changes apply through the existing persona hot load path on subsequent dispatches. The preview intentionally supports a Markdown subset and displays raw HTML as text.
