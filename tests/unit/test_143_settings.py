"""#143: one Settings page. Tabs, descriptions, redirects, "Used by", and the page's pure functions.

The browser behaviour (unsaved-changes prompt, review, conflict, deep links) is in
tests/e2e/test_143_settings.py. This file covers the server side and the pure
JavaScript functions, which run under node.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from coordinare import vocabulary
from coordinare.config import CoordinareConfiguration
from coordinare.config_usage import UsedBy, routing_used_by, usage_index
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.dashboard import (
    _DASHBOARD_HTML,
    _DASHBOARD_JS_SOURCES,
)
from coordinare.mcp.tools._registry import DEFERRED_PAGE_PATHS
from tests.unit.dashboard_js_slices import shipped_function
from tests.unit.test_dashboard_config_api import _make_client

_TABS = {
    "global": "General",
    "personas": "Agents",
    "symphonies": "Symphonies",
    "endpoints": "Model servers",
    "model_endpoints": "Models",
    "modes": "Modes",
    "routing": "Routing",
}


def _config(performers: dict[str, Any], symphony_overrides: dict[str, Any] | None = None) -> CoordinareConfiguration:
    raw: dict[str, Any] = {
        "github_org": "acme",
        "github_token": "ghp_fixturetoken",
        "human_reviewers": ["alice"],
        "endpoints": [
            {"name": "local-vllm", "kind": "vllm", "base_url": "http://localhost:8000"},
            {"name": "openai-native", "kind": "openai"},
        ],
        "model_endpoints": [
            {"name": "qwen-coder", "endpoint": "local-vllm", "model": "qwen2.5-coder"},
            {"name": "gpt", "endpoint": "openai-native", "model": "gpt-4o"},
            {"name": "spare", "endpoint": "local-vllm", "model": "spare-model"},
        ],
        "modes": [
            {"name": "single-qwen", "strategy": "single", "tool": "qwen-coder"},
            {"name": "gpt-only", "strategy": "single", "tool": "gpt"},
            {"name": "idle", "strategy": "single", "tool": "spare"},
        ],
        "performers": performers,
        "symphonies": [
            {"name": "demo", "github_project_number": 100},
            {"name": "other", "github_project_number": 101,
             "overrides": symphony_overrides or {}},
        ],
        "orchestra": {"mode": "shared_pool"},
    }
    return CoordinareConfiguration(**coerce_multi_symphony_raw(raw))


_PERFORMERS = {
    "implementer": {"backend": "opencode", "mode": "single-qwen"},
}


def _names(items: list[UsedBy]) -> list[tuple[str, str]]:
    return [(item.kind, item.name) for item in items]


# --- "Used by": one computation, several kinds of item ----------------------------


def test_a_persona_is_used_by_its_stage_and_every_symphony_that_runs_its_role() -> None:
    index = usage_index(_config(_PERFORMERS, {"performers": {"implementer": {"mode": "gpt-only"}}}))
    assert _names(index[("persona", "implementer")]) == [
        ("stage", "implementing"), ("symphony", "demo"), ("symphony", "other"),
    ]


def test_a_persona_whose_role_is_not_configured_is_not_used() -> None:
    index = usage_index(_config(_PERFORMERS))
    assert index.get(("persona", "reviewer"), []) == []


def test_a_mode_is_used_by_the_stages_and_symphonies_that_pick_it() -> None:
    index = usage_index(_config(_PERFORMERS, {"performers": {"implementer": {"mode": "gpt-only"}}}))
    assert _names(index[("mode", "single-qwen")]) == [("stage", "implementing"), ("symphony", "demo")]
    assert _names(index[("mode", "gpt-only")]) == [("stage", "implementing"), ("symphony", "other")]
    assert index[("mode", "idle")] == []


def test_a_model_is_used_by_the_modes_that_name_it_and_through_them_by_their_users() -> None:
    index = usage_index(_config(_PERFORMERS))
    assert _names(index[("model", "qwen-coder")]) == [
        ("mode", "single-qwen"), ("stage", "implementing"), ("symphony", "demo"), ("symphony", "other"),
    ]
    # Only an unused mode names the spare model, so its only user is that mode, which nothing uses.
    assert _names(index[("model", "spare")]) == [("mode", "idle")]


def test_a_model_server_is_used_by_its_models_and_their_users() -> None:
    index = usage_index(_config(_PERFORMERS, {"performers": {"implementer": {"mode": "gpt-only"}}}))
    assert _names(index[("model_server", "openai-native")]) == [
        ("model", "gpt"), ("mode", "gpt-only"), ("stage", "implementing"), ("symphony", "other"),
    ]
    assert _names(index[("model_server", "local-vllm")]) == [
        ("model", "qwen-coder"), ("mode", "single-qwen"), ("stage", "implementing"), ("symphony", "demo"),
        ("model", "spare"), ("mode", "idle"),
    ]


def test_routing_applies_only_to_the_roles_whose_backend_and_model_match_the_entry() -> None:
    config = _config(_PERFORMERS, {"performers": {"implementer": {"mode": "gpt-only"}}})
    # The demo symphony runs the implementer on qwen2.5-coder; the other symphony
    # switched to a mode that does not use that model, so it is not a user of this entry.
    assert _names(routing_used_by(config, "opencode", "qwen2.5-coder")) == [
        ("stage", "implementing"), ("symphony", "demo"),
    ]
    assert routing_used_by(config, "anthropic", "qwen2.5-coder") == []
    assert routing_used_by(config, "opencode", "no-such-model") == []


# --- the API: tabs, descriptions and "Used by" in the snapshot --------------------


@pytest.fixture
def settings_client(temp_config_path: Path) -> TestClient:
    raw = yaml.safe_load(temp_config_path.read_text())
    raw["performers"] = _PERFORMERS
    return _make_client(temp_config_path, raw)


def test_each_tab_has_its_name_and_one_line_of_description(settings_client: TestClient) -> None:
    sections = {s["id"]: s for s in settings_client.get("/api/config/all").json()["sections"]}
    assert {sid: sections[sid]["title"] for sid in _TABS} == _TABS
    for sid in _TABS:
        description = sections[sid]["description"]
        assert description, f"{sid} has no description"
        assert "\n" not in description and len(description) < 200, sid


def test_the_snapshot_says_what_uses_each_item(settings_client: TestClient) -> None:
    sections = {s["id"]: s for s in settings_client.get("/api/config/all").json()["sections"]}
    personas = {item["id"]: item for item in sections["personas"]["items"]}
    assert [u["name"] for u in personas["implementer"]["used_by"]] == ["implementing", "demo"]
    assert personas["reviewer"]["used_by"] == []
    models = {item["id"]: item for item in sections["model_endpoints"]["items"]}
    assert [u["kind"] for u in models["qwen-coder"]["used_by"]] == ["mode", "stage", "symphony"]


def test_model_servers_and_models_are_named_for_what_they_are(settings_client: TestClient) -> None:
    sections = {s["id"]: s for s in settings_client.get("/api/config/all").json()["sections"]}
    assert "Where model calls go" in sections["endpoints"]["description"]
    assert "Modes choose" in sections["model_endpoints"]["description"]


# --- redirects: the retired Global Config address moves to Settings, General ----


def test_the_global_config_address_redirects_to_the_general_tab(temp_config_path: Path) -> None:
    client = _make_client(temp_config_path, yaml.safe_load(temp_config_path.read_text()))
    response = client.get("/admin/config", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/config#global"


def test_the_redirect_keeps_the_query_string(temp_config_path: Path) -> None:
    client = _make_client(temp_config_path, yaml.safe_load(temp_config_path.read_text()))
    response = client.get("/admin/config?from=nav", follow_redirects=False)
    assert response.headers["location"] == "/config?from=nav#global"


def test_the_settings_page_itself_is_served_at_its_address(temp_config_path: Path) -> None:
    client = _make_client(temp_config_path, yaml.safe_load(temp_config_path.read_text()))
    response = client.get("/config")
    assert response.status_code == 200
    assert response.text == _DASHBOARD_HTML


def test_the_mcp_registry_still_lists_both_addresses() -> None:
    assert "/admin/config" in DEFERRED_PAGE_PATHS
    assert "/config" in DEFERRED_PAGE_PATHS


# --- the page: one Settings item in the nav, the duplicate pages gone -------------


def test_the_nav_has_one_settings_item_and_no_global_config_item() -> None:
    assert 'href="/admin/config"' not in _DASHBOARD_HTML
    assert "Global Config" not in _DASHBOARD_HTML
    assert 'aria-label="Settings"' in _DASHBOARD_HTML
    assert 'href="/config"' in _DASHBOARD_HTML
    assert "admin-config-page" not in _DASHBOARD_HTML


def test_the_personas_page_is_read_only_and_links_to_its_settings_item() -> None:
    app = _DASHBOARD_JS_SOURCES["app"]
    loader = shipped_function("app", "loadPersonasPage")
    assert "persona-save-btn" not in loader
    assert "persona-reset-btn" not in loader
    assert "<textarea" not in loader
    assert "/config#personas/" in loader
    assert "Edit in Settings" in loader
    assert "savePersona" not in app and "resetPersona" not in app


def test_the_old_persona_editor_is_gone_from_the_main_page() -> None:
    assert "personas-main-card" not in _DASHBOARD_HTML
    assert "personas-section" not in _DASHBOARD_HTML


def test_the_retired_addresses_are_redirected_on_the_client_too() -> None:
    redirect = shipped_function("app", "redirectRetiredPages")
    assert "'/admin/config'" in redirect
    assert "history.replaceState" in redirect
    assert "'/config' + location.search + '#global'" in redirect


def test_the_glossary_explains_the_settings_terms() -> None:
    for code in ("settings", "model_server", "model", "mode", "routing"):
        assert code in vocabulary.CONCEPTS, code
    assert vocabulary.CONCEPTS["model_server"].label == "Model server"
    assert vocabulary.CONCEPTS["mode"].description.startswith("How a performer role")


# --- the page's pure functions, run under node ----------------------------------

_NODE = shutil.which("node")

_CONFIG_PURE = (
    "cfgDiffText", "cfgDiffRows", "cfgConflictMessage", "cfgReadOnlyNote",
    "cfgUsedByLabel",
)


def _run_node(tmp_path: Path, body: str) -> None:
    assert _NODE is not None, "node is required for the dashboard JS gates"
    functions = "\n\n".join(shipped_function("config", name) for name in _CONFIG_PURE)
    stub = "function vocabularyKindLabel(kind, code) { return {implementing: 'Implement'}[code] || code; }\n"
    script = tmp_path / "check.js"
    script.write_text(
        stub + functions + "\n\n" + body + "\nconsole.log('ok');\n",
        encoding="utf-8",
    )
    result = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_diff_shows_old_and_new_for_each_changed_setting_only(tmp_path: Path) -> None:
    _run_node(tmp_path, """
const rows = cfgDiffRows([
  {label: 'Poll interval', before: 30, after: 45, secret: false},
  {label: 'Log level', before: 'info', after: 'info', secret: false},
  {label: 'Assignee filter', before: null, after: 'alice', secret: false},
  {label: 'Reviewers', before: ['a'], after: ['a', 'b'], secret: false},
]);
const want = [
  {label: 'Poll interval', before: '30', after: '45', secret: false},
  {label: 'Assignee filter', before: '(unset)', after: 'alice', secret: false},
  {label: 'Reviewers', before: 'a', after: 'a, b', secret: false},
];
if (JSON.stringify(rows) !== JSON.stringify(want)) {
  throw new Error('diff rows: ' + JSON.stringify(rows));
}
""")


def test_a_secret_is_reported_as_changed_and_never_shows_either_value(tmp_path: Path) -> None:
    _run_node(tmp_path, """
const rows = cfgDiffRows([
  {label: 'GitHub token', before: 'ghp_OLDSECRET', after: 'ghp_NEWSECRET', secret: true},
]);
const text = JSON.stringify(rows);
if (rows.length !== 1 || rows[0].before !== '(hidden)' || rows[0].after !== '(hidden)') {
  throw new Error('secret row: ' + text);
}
if (text.indexOf('OLDSECRET') !== -1 || text.indexOf('NEWSECRET') !== -1) {
  throw new Error('secret value leaked: ' + text);
}
""")


def test_a_conflict_is_said_in_plain_words_with_the_edits_kept(tmp_path: Path) -> None:
    _run_node(tmp_path, """
const message = cfgConflictMessage(409, {conflict: true, error: 'stale'});
for (const phrase of ['Someone else changed these settings', 'nothing was saved', 'Your edits are still here', 'Reload and reapply']) {
  if (message.indexOf(phrase) === -1) throw new Error('missing "' + phrase + '": ' + message);
}
if (message.indexOf('stale') !== -1) throw new Error('raw server text leaked: ' + message);
const missing = cfgConflictMessage(428, {});
if (missing.indexOf('Reload and reapply') === -1) throw new Error('428 wording: ' + missing);
""")


def test_a_clash_that_is_not_a_version_keeps_the_servers_own_words(tmp_path: Path) -> None:
    _run_node(tmp_path, """
if (cfgConflictMessage(409, {error: 'Symphony already exists'}) !== 'Symphony already exists') {
  throw new Error('duplicate name reworded');
}
if (cfgConflictMessage(500, {}) !== 'Error 500') throw new Error('fallback');
""")


def test_a_read_only_setting_says_why(tmp_path: Path) -> None:
    _run_node(tmp_path, """
if (cfgReadOnlyNote({}, 'symphonies') !== "Edit this on the symphony's own page.") throw new Error('symphony');
if (cfgReadOnlyNote({invalid: true}, 'global').indexOf('fails validation') === -1) throw new Error('invalid');
if (cfgReadOnlyNote({is_env_placeholder: true}, 'global').indexOf('environment variable') === -1) throw new Error('env');
""")


def test_used_by_names_each_kind_in_words(tmp_path: Path) -> None:
    _run_node(tmp_path, """
const cases = [
  [{kind: 'stage', name: 'implementing'}, 'Implement stage'],
  [{kind: 'symphony', name: 'demo'}, 'symphony demo'],
  [{kind: 'mode', name: 'single-qwen'}, 'mode single-qwen'],
  [{kind: 'model', name: 'qwen-coder'}, 'model qwen-coder'],
  [{kind: 'model_server', name: 'local-vllm'}, 'model server local-vllm'],
];
for (const [use, want] of cases) {
  const got = cfgUsedByLabel(use);
  if (got !== want) throw new Error('label: ' + got + ' != ' + want);
}
""")


def test_the_settings_page_has_no_en_or_em_dash_in_its_new_functions() -> None:
    """Jason's rule: no em or en dashes in text this change writes."""
    for name in ("cfgDiffText", "cfgDiffRows", "cfgConflictMessage", "cfgReadOnlyNote", "cfgUsedByLabel",
                 "cfgUsedByHtml", "cfgShowReview", "cfgReapplyPending", "cfgPersonaBlock",
                 "cfgPersonasSection", "cfgFocusItem", "cfgSyncTab", "cfgLeaveAllowed"):
        body = shipped_function("config", name)
        assert chr(0x2014) not in body and chr(0x2013) not in body, name
