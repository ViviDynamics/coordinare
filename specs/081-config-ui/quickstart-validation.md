# Quickstart Validation Record (T045)

**Feature**: 081-config-ui | **Date**: 2026-06-06 | **Validator**: speckit.implement Phase 6

This records the end-to-end validation of [quickstart.md](quickstart.md). A live dashboard
cannot be driven headlessly in this environment, so each quickstart step is validated by the
deterministic automated test(s) that exercise the same backing call/behavior against a temp
`config.yaml` fixture (in-process FastAPI `TestClient`, MagicMock daemon — no live performer).
Manual screen-reader/browser certification of the rendered view is tracked separately in the
[UX accessibility checklist](checklists/ux-accessibility.md).

**Suites** (run separately — coordinare vs. performer conftest collision):
`tests/unit/test_dashboard_config_api.py`, `tests/unit/test_dashboard_config_integration.py`,
`tests/unit/test_config_write_service.py`, `tests/unit/test_routing_config_service.py`,
`tests/unit/test_config_descriptors.py`. Command: `.venv/bin/pytest tests/unit/`.

| Quickstart step | Behavior | Covering test(s) | Result |
|---|---|---|---|
| §1 View whole config | all 7 sections + content_hashes + version; secrets masked, `${VAR}` raw | `test_config_all_returns_all_seven_sections`, `test_config_all_includes_content_hashes_and_version`, `test_config_all_masks_secret_but_preserves_env_placeholder`, `test_build_snapshot_has_all_seven_sections`, `test_config_section_returns_single_section`, `test_config_section_unknown_returns_404` | PASS |
| §1 Invalid-on-disk | bad field renders read-only + banner, valid fields normal | `test_best_effort_load_marks_invalid_field_read_only` | PASS |
| §2 Edit hot-reloadable | `applied: hot_reloaded`, version bump; out-of-range → 422 inline, no write | `test_put_section_global_hot_reloaded`, `test_put_section_validation_error_422`, `test_save_section_persists_and_classifies`, `test_classify_applied_hot_reloaded_for_tunable_field` | PASS |
| §2 Restart-required | startup-bound field → `staged_restart` | `test_put_section_restart_required_field_staged`, `test_classify_applied_staged_restart_for_process_binding` | PASS |
| §3 Optimistic concurrency | external on-disk edit → 409 conflict | `test_put_section_conflict_409`, `test_guard_concurrency_rejects_on_mismatch`, `test_save_section_conflict_on_stale_hash` | PASS |
| §4 Catalog CRUD | create/update/delete endpoints/model_endpoints/modes | `test_catalog_get_endpoints_with_refs`, `test_catalog_post_create_endpoint`, `test_catalog_put_update_endpoint`, `test_create_catalog_item_persists`, `test_update_catalog_item_persists` | PASS |
| §4 Referential integrity | unknown reference → 422; delete-while-referenced → 409 naming referrer | `test_catalog_post_unknown_reference_422`, `test_catalog_delete_referenced_409`, `test_catalog_delete_unreferenced_ok`, `test_create_catalog_item_unknown_reference_rejected`, `test_delete_referenced_catalog_item_blocked` | PASS |
| §5 Routing CRUD | create/update/delete → `applied: staged_next_job` | `test_routing_post_create_staged_next_job`, `test_routing_put_update_entry`, `test_routing_delete_entry`, `test_create_routing_entry_appends_and_stages_next_job` | PASS |
| §5 Routing validation | reroute+normalizers / normalize-empty / unknown normalizer / bad wire_format → inline error | `test_routing_post_invalid_422`, `test_validate_routing_entry_reroute_with_normalizers_rejected`, `test_validate_routing_entry_normalize_without_normalizers_rejected`, `test_validate_routing_entry_unknown_normalizer_rejected`, `test_validate_routing_entry_bad_wire_format_rejected` | PASS |
| §5.1 Routing empty state | `routing_available` false → read-only guidance, not error | `test_routing_get_unavailable_empty_state`, `test_config_all_routing_unavailable_empty_state`, `test_routing_write_unavailable_403`, `test_read_routing_none_location_is_empty_state` | PASS |
| §6 Secret safety | masked-unchanged secret = no-op; no mask/plaintext on disk or in payload | `test_save_section_masked_secret_is_noop`, `test_save_masked_secret_never_writes_mask_or_plaintext`, `test_serialize_value_masks_secret_literal`, `test_serialize_value_preserves_env_placeholder_even_when_secret` | PASS |
| §7 Performance (SC-010) | GET /api/config/all < 300 ms p95; save round-trip < 500 ms p95 | `test_get_config_all_under_300ms_p95`, `test_section_save_round_trip_under_500ms_p95` | PASS |
| Reload-failure integrity (FR-015) | write persists, in-memory retained, `staged_restart`, secret-free message | `test_reload_failure_retains_prior_config_and_stages_restart`, `test_section_save_writes_and_triggers_reload` | PASS |
| Legacy endpoint contract (FR-014/018) | pre-081 `/api/config/global|effective|reload` + `/api/personas/*` unchanged | `test_legacy_get_config_global_returns_exact_field_slice`, `test_legacy_put_config_global_persists_and_reports_saved`, `test_legacy_get_config_effective_default_mode`, `test_legacy_post_config_reload_accepted`, `test_legacy_personas_list_returns_all_roles`, `test_legacy_persona_put_then_delete_round_trip` | PASS |
| Atomicity (SC-007) | crash before `os.replace` → original byte-for-byte intact, no temp left | `test_atomic_write_yaml_crash_before_replace_leaves_original_intact`, `test_atomic_write_yaml_leaves_no_temp_files`, `test_atomic_write_yaml_preserves_mode` | PASS |

## Outcome

All 7 quickstart steps (plus the FR-015 reload-failure, FR-014/018 legacy-contract, and
SC-007 atomicity guarantees) are covered by green automated tests. See the suite run captured
in the Phase 6 implementation commit. Live manual confirmation against a running dashboard is
deferred to operator sign-off using the same step list; the automated coverage above is the
gating evidence for implementation completion.
