from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinare.config import (
    PERSONA_MAX_LENGTH,
    PersonaConfig,
    PersonasConfig,
    ProjectConfiguration,
    SymphonyConfig,
)
from coordinare.dashboard import _DASHBOARD_JS_SOURCES
from coordinare.graph.nodes.dispatch_performer import PERSONA_LANGUAGE_SUFFIX, _base_card_context
from coordinare.services.persona_service import VALID_ROLES, get_effective_instructions
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
