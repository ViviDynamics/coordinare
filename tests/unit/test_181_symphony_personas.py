from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from structlog.testing import capture_logs

from coordinare.config import (
    PERSONA_MAX_LENGTH,
    CoordinareConfiguration,
    PersonaConfig,
    PersonasConfig,
    ProjectConfiguration,
    SymphonyConfig,
)
from coordinare.dashboard import _DASHBOARD_JS_SOURCES
from coordinare.doctor import Status
from coordinare.graph.nodes.dispatch_performer import (
    PERSONA_LANGUAGE_SUFFIX,
    _base_card_context,
    _resolve_persona_slice_and_behavior,
)
from coordinare.launch_preflight import run_preflight
from coordinare.services.persona_service import VALID_ROLES, get_effective_instructions
from coordinare.start_checks import StartCheckError, validate_start_time
from tests.unit.dashboard_js_slices import label_helpers_js, shipped_function
from tests.unit.test_221_symphony_editor import _BASE, _SECRET, _client, _write

_TEXT = "  # Symphony\r\n\r\nExact **source**.  \r\n"
_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("role", sorted(VALID_ROLES))
def test_symphony_replaces_only_one_roles_instructions(role: str) -> None:
    global_config = ProjectConfiguration(
        github_org="acme", github_token="token", github_project_number=1, human_reviewers=["alice"],
        personas=PersonasConfig(**{role: PersonaConfig(instructions="global")}),
    )
    symphony = SymphonyConfig(name="demo", github_project_number=2, personas={role: {"instructions": _TEXT}})
    effective = symphony.effective_config(global_config)
    assert get_effective_instructions(role, effective.personas) == _TEXT
    assert get_effective_instructions(role, global_config.personas) == "global"
    for other in VALID_ROLES - {role}:
        assert getattr(effective.personas, other) == getattr(global_config.personas, other)


@pytest.mark.parametrize("text", ["", " \n\t"])
def test_empty_symphony_inherits_global_instructions_and_scope(text: str) -> None:
    global_config = ProjectConfiguration(
        github_org="acme", github_token="token", github_project_number=1, human_reviewers=["alice"],
        personas={"qa": {"instructions": "global QA", "scope_behavior": {"skim": {"max_tool_calls": 3}}}},
    )
    for instructions in (text, _TEXT):
        symphony = SymphonyConfig(name="demo", github_project_number=2, personas={"qa": {"instructions": instructions}})
        effective = symphony.effective_config(global_config)
        assert effective.personas.qa.instructions == (instructions if instructions.strip() else "global QA")
        assert effective.personas.qa.scope_behavior == global_config.personas.qa.scope_behavior


def test_override_set_clear_validate_and_conflict_preserve_secrets(tmp_path: Path) -> None:
    raw = {**_BASE, "symphonies": [{"name": "demo", "github_project_number": 100,
                                    "overrides": {"github_token": _SECRET}}]}
    path = _write(tmp_path / "config.yaml", raw)
    client, _ = _client(path, raw)
    loaded = client.get("/api/symphonies/demo")
    version = loaded.headers["etag"]
    body = {"personas": {"qa": {"instructions": _TEXT}}, "overrides": loaded.json()["overrides"]}
    before = path.read_bytes()
    validated = client.post("/api/symphonies/demo/validate", json=body)
    assert validated.status_code == 200 and validated.json()["valid"] is True
    assert path.read_bytes() == before
    saved = client.put("/api/symphonies/demo", json=body, headers={"If-Match": version})
    assert saved.status_code == 200, saved.text
    assert _SECRET not in saved.text
    assert yaml.safe_load(path.read_text())["symphonies"][0]["personas"]["qa"]["instructions"] == _TEXT
    stale = client.put("/api/symphonies/demo", json={"personas": None}, headers={"If-Match": version})
    assert stale.status_code == 409 and stale.json()["conflict"] is True
    cleared = client.put("/api/symphonies/demo", json={"personas": None}, headers={"If-Match": saved.headers["etag"]})
    assert cleared.status_code == 200
    stored = yaml.safe_load(path.read_text())["symphonies"][0]
    assert not stored.get("personas")
    assert stored["overrides"]["github_token"] == _SECRET


def test_shipped_symphony_persona_node_checks() -> None:
    code = "\n".join([
        shipped_function("helpers", "esc"), label_helpers_js(), _DASHBOARD_JS_SOURCES["personas"],
        *(shipped_function("app", name) for name in ("symCopy", "symRequestBody", "symSave")),
        shipped_function("app", "symValidateAndSave"),
        shipped_function("config", "cfgLoadSymphony"),
        *(shipped_function("config", name) for name in ("cfgRememberSymphony", "cfgSymphonyHasDrafts")),
        *(shipped_function("config", name) for name in ("cfgSecretChanged", "cfgDiffText", "cfgDiffRows")),
        (_ROOT / "tests/js/symphony_181_checks.js").read_text(),
    ])
    result = subprocess.run(["node", "-"], input=code, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_dispatch_uses_current_symphony_override_after_global_hot_reload(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write(path, {**_BASE, "personas": {"implementer": {"instructions": "fresh global"}}})
    local = SymphonyConfig(name="demo", github_project_number=100, personas={"implementer": {"instructions": _TEXT}})
    other = SymphonyConfig(name="other", github_project_number=101)
    state = {"config_path": path, "current_symphony": "demo", "symphony_configs": {"demo": local, "other": other}}
    payload, _ = _base_card_context(state, {}, "card", "implementing")
    assert payload["persona_instructions"] == _TEXT + PERSONA_LANGUAGE_SUFFIX
    state["current_symphony"] = "other"
    payload, _ = _base_card_context(state, {}, "card", "implementing")
    assert payload["persona_instructions"] == "fresh global" + PERSONA_LANGUAGE_SUFFIX
    state["current_symphony"] = "demo"
    local.personas = None
    payload, _ = _base_card_context(state, {}, "card", "implementing")
    assert payload["persona_instructions"] == "fresh global" + PERSONA_LANGUAGE_SUFFIX


@pytest.mark.parametrize("personas", [
    {"qa": {"instructions": "x" * (PERSONA_MAX_LENGTH + 1)}},
    {"qa": {"instructions": ["wrong type"]}},
    {"unknown": {"instructions": "text"}},
])
def test_invalid_persona_dry_run_and_save_write_nothing(tmp_path: Path, personas: dict[str, object]) -> None:
    path = _write(tmp_path / "config.yaml", _BASE)
    client, _ = _client(path, _BASE)
    version = client.get("/api/symphonies/demo").headers["etag"]
    before = path.read_bytes()
    validated = client.post("/api/symphonies/demo/validate", json={"personas": personas})
    assert validated.status_code == 400
    assert validated.json()["valid"] is False
    saved = client.put("/api/symphonies/demo", json={"personas": personas}, headers={"If-Match": version})
    assert saved.status_code == 400
    assert path.read_bytes() == before


def _unknown_role_config() -> CoordinareConfiguration:
    return CoordinareConfiguration(
        global_config=ProjectConfiguration(
            github_org="acme", github_token="token", github_project_number=1, human_reviewers=["alice"],
        ),
        symphonies=[SymphonyConfig(name="demo", github_project_number=2, personas={"bad_role": {"instructions": "secret"}})],
    )


def test_unknown_persona_role_start_checks_names_symphony_key_and_valid_roles() -> None:
    config = _unknown_role_config()
    with pytest.raises(StartCheckError) as caught:
        validate_start_time(config.global_config, config)
    assert caught.value.field == "symphonies[0].personas"
    message = str(caught.value)
    assert "demo" in message and "bad_role" in message
    assert all(role in message for role in VALID_ROLES)
    assert "secret" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("bypass_start_checks", [False, True])
async def test_unknown_persona_role_preflight_returns_fail(
    monkeypatch: pytest.MonkeyPatch, bypass_start_checks: bool,
) -> None:
    config = _unknown_role_config()
    probes = Mock(side_effect=AssertionError("Invalid config must fail before probes"))
    monkeypatch.setattr("coordinare.launch_preflight.check_directories", probes)
    if bypass_start_checks:
        monkeypatch.setattr("coordinare.launch_preflight.validate_start_time", lambda *args: None)
    report = await run_preflight(config.global_config, config)
    assert len(report.results) == 1
    result = report.results[0]
    assert result.status is Status.FAIL
    assert "demo" in result.detail and "bad_role" in result.detail
    assert all(role in result.detail for role in VALID_ROLES)
    assert "config validate" in result.fix
    probes.assert_not_called()


@pytest.mark.parametrize("route", ["validate", "update", "create"])
def test_unknown_persona_role_response_never_echoes_request_key(tmp_path: Path, route: str) -> None:
    path = _write(tmp_path / "config.yaml", _BASE)
    client, _ = _client(path, _BASE)
    key = "private-requester-role-key"
    body = {"personas": {key: {"instructions": "secret"}}}
    if route == "validate":
        response = client.post("/api/symphonies/demo/validate", json=body)
    elif route == "update":
        response = client.put("/api/symphonies/demo", json=body)
    else:
        response = client.post("/api/symphonies", json={**body, "name": "new", "github_project_number": 3})
    assert response.status_code == 400
    assert key not in response.text
    assert "Unknown persona role" in response.text
    assert all(role in response.text for role in VALID_ROLES)


@pytest.mark.parametrize("route", ["validate", "update", "create"])
def test_overlength_persona_logs_exclude_source_and_exception(tmp_path: Path, route: str) -> None:
    path = _write(tmp_path / "config.yaml", _BASE)
    client, _ = _client(path, _BASE)
    secret = "PRIVATE-PERSONA-SECRET"
    body = {"personas": {"qa": {"instructions": secret + "x" * PERSONA_MAX_LENGTH}}}
    with capture_logs() as logs:
        if route == "validate":
            response = client.post("/api/symphonies/demo/validate", json=body)
        elif route == "update":
            response = client.put("/api/symphonies/demo", json=body)
        else:
            response = client.post("/api/symphonies", json={**body, "name": "new", "github_project_number": 3})
    assert response.status_code == 400
    assert secret not in response.text
    assert secret not in repr(logs)
    for entry in logs:
        assert not entry.get("exc_info")
        assert "exception" not in entry
    if route != "validate":
        failure = next(entry for entry in logs if entry["event"] == "symphony_effective_config_failed")
        assert failure["error_type"] == "ValidationError"
        assert failure["field_locations"] == ["instructions"]


@pytest.mark.parametrize("route", ["update", "create", "delete"])
def test_symphony_persistence_failure_logs_only_error_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str,
) -> None:
    path = _write(tmp_path / "config.yaml", _BASE)
    client, _ = _client(path, _BASE)
    version = client.get("/api/symphonies/demo").headers["etag"]
    secret = "PRIVATE-PERSISTENCE-INPUT"
    monkeypatch.setattr(
        "coordinare.dashboard.routers.symphonies._persist_symphony_configs",
        Mock(side_effect=RuntimeError(secret)),
    )
    with capture_logs() as logs:
        if route == "update":
            response = client.put("/api/symphonies/demo", json={"personas": {"qa": {"instructions": _TEXT}}},
                                  headers={"If-Match": version})
        elif route == "create":
            response = client.post("/api/symphonies", json={"name": "new", "github_project_number": 3},
                                   headers={"If-Match": version})
        else:
            response = client.delete("/api/symphonies/demo", headers={"If-Match": version})
    assert response.status_code == 500
    assert secret not in response.text
    assert secret not in repr(logs)
    failure = next(entry for entry in logs if entry["event"] == "symphony_persist_failed")
    assert not failure.get("exc_info")
    assert failure["error_type"] == "RuntimeError"
    assert failure["field_locations"] == []


@pytest.mark.parametrize("text", ["", " \n\t"])
def test_blank_override_dispatch_inherits_global_instructions(text: str) -> None:
    config = ProjectConfiguration(
        github_org="acme", github_token="token", github_project_number=1, human_reviewers=["alice"],
        personas={"implementer": {"instructions": "global implementer"}},
    )
    local = SymphonyConfig(name="demo", github_project_number=2, personas={"implementer": {"instructions": text}})
    state = {"config": config, "current_symphony": "demo", "symphony_configs": {"demo": local}}
    payload, role = _base_card_context(state, {}, "card", "implementing")
    assert role == "implementer"
    assert payload["persona_instructions"] == "global implementer" + PERSONA_LANGUAGE_SUFFIX


def test_dispatch_scope_tiering_stays_global_with_symphony_override() -> None:
    config = ProjectConfiguration(
        github_org="acme", github_token="token", github_project_number=1, human_reviewers=["alice"],
        personas={"qa": {"scope_behavior": {"skim": {"max_tool_calls": 3, "prompt_addon": "global tier"}}}},
    )
    local = SymphonyConfig(name="demo", github_project_number=2, personas={"qa": {
        "instructions": "local QA", "scope_behavior": {"skim": {"max_tool_calls": 9, "prompt_addon": "local tier"}},
    }})
    scope = {"depth": "skim"}
    state = {"config": config, "current_symphony": "demo", "symphony_configs": {"demo": local},
             "active_sessions": {"card": {"persona_scope": {"personas": {"qa": scope}}}}}
    payload, _ = _base_card_context(state, {}, "card", "qa")
    assert payload["persona_instructions"] == "local QA" + PERSONA_LANGUAGE_SUFFIX
    actual_slice, behavior = _resolve_persona_slice_and_behavior(state, "card", "qa")
    assert actual_slice == scope
    assert behavior is config.personas.qa.scope_behavior
    assert behavior.skim.max_tool_calls == 3
    assert behavior.skim.prompt_addon == "global tier"
