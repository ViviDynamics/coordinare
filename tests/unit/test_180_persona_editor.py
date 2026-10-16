from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from coordinare.config import PERSONA_MAX_LENGTH
from coordinare.dashboard import _DASHBOARD_JS_SOURCES, guards
from coordinare.services import config_write_service
from coordinare.services.persona_service import save_persona
from tests.unit.dashboard_js_slices import shipped_function
from tests.unit.test_dashboard import _make_personas_app

_ROOT = Path(__file__).resolve().parents[2]
_TEXT = "  # Instructions\r\n\r\nUse **care** and `code`.  \r\n\tUnicode: café\n\n"
_CONFLICT = 409
_FORBIDDEN = 403
_READ_ONLY_MESSAGE = "Config is mounted read-only; persona edits need a writable config (see the Helm chart notes)"


@pytest.mark.parametrize("method", ["put", "delete"])
def test_persona_change_after_router_guard_is_refused(tmp_path: Path, method: str) -> None:
    client = _make_personas_app(tmp_path)
    path = tmp_path / "config.yaml"
    version = client.get("/api/personas").headers["etag"]
    concurrent = path.read_text() + "\n# changed after router guard\n"
    original_guard = guards._version_refusal

    def check_then_change(config_path: Path, supplied: str | None) -> object:
        refusal = original_guard(config_path, supplied)
        assert refusal is None
        path.write_text(concurrent)
        return refusal

    with patch("coordinare.dashboard.routers.personas._version_refusal", side_effect=check_then_change):
        response = client.request(method, "/api/personas/implementer", json={"instructions": "new"},
                                  headers={"If-Match": version})
    assert response.status_code == _CONFLICT
    assert response.json()["conflict"] is True
    assert path.read_text() == concurrent


@pytest.mark.parametrize("method", ["put", "delete"])
@pytest.mark.parametrize("target", ["file", "directory", "mount"])
def test_persona_read_only_config_refused_before_write(tmp_path: Path, method: str, target: str) -> None:
    client = _make_personas_app(tmp_path)
    path = tmp_path / "config.yaml"
    version = client.get("/api/personas").headers["etag"]
    before = path.read_bytes()
    readonly_path = path if target == "file" else tmp_path
    original_mode = readonly_path.stat().st_mode
    write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    mount_info = os.statvfs(path)
    readonly_mount = SimpleNamespace(f_flag=mount_info.f_flag | os.ST_RDONLY)
    if target != "mount":
        readonly_path.chmod(original_mode & ~write_bits)
    try:
        with (
            patch.object(os, "statvfs", return_value=readonly_mount if target == "mount" else mount_info),
            patch.object(config_write_service, "atomic_write_yaml", wraps=config_write_service.atomic_write_yaml) as writer,
        ):
            response = client.request(method, "/api/personas/implementer", json={"instructions": "new"},
                                      headers={"If-Match": version})
        assert response.status_code == _FORBIDDEN
        assert response.json()["error"] == _READ_ONLY_MESSAGE
        writer.assert_not_called()
        assert path.read_bytes() == before
    finally:
        readonly_path.chmod(original_mode)


def test_shipped_editor_node_checks() -> None:
    source = _DASHBOARD_JS_SOURCES.get("personas", "")
    helpers = shipped_function("helpers", "esc")
    shared = "\n".join(shipped_function("config", name) for name in (
        "cfgCoerce", "cfgParseSaved", "cfgUnset", "cfgSameValue", "cfgEntries", "cfgChanged", "cfgPendingFor",
        "cfgTabId", "cfgPanelId", "cfgSelectTab",
    ))
    checks = (_ROOT / "tests/js/persona_180_checks.js").read_text()
    result = subprocess.run(["node", "-"], input=helpers + source + shared + checks, text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_persona_save_uses_shared_atomic_writer(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("github_org: acme\ngithub_project_number: 1\nhuman_reviewers: [alice]\ngithub_token: tok\n")
    with patch.object(config_write_service, "atomic_write_yaml", wraps=config_write_service.atomic_write_yaml) as writer:
        save_persona("implementer", _TEXT, path)
    writer.assert_called_once()
    assert yaml.safe_load(path.read_text())["personas"]["implementer"]["instructions"] == _TEXT


def test_untouched_api_text_round_trips_exactly(tmp_path: Path) -> None:
    path = tmp_path / "exact.yaml"
    path.write_text("github_org: acme\ngithub_project_number: 1\nhuman_reviewers: [alice]\ngithub_token: tok\n")
    client = _make_personas_app(tmp_path, config_path=path)
    version = client.get("/api/personas").headers["etag"]
    saved = client.put("/api/personas/implementer", json={"instructions": _TEXT}, headers={"If-Match": version})
    assert saved.status_code == 200, saved.text
    loaded = next(p for p in client.get("/api/personas").json() if p["role"] == "implementer")
    assert loaded["instructions"].encode() == _TEXT.encode()
    again = client.put("/api/personas/implementer", json={"instructions": loaded["instructions"]},
                       headers={"If-Match": saved.headers["etag"]})
    assert again.status_code == 200, again.text
    assert yaml.safe_load(path.read_text())["personas"]["implementer"]["instructions"].encode() == _TEXT.encode()


@pytest.mark.parametrize("method", ["put", "delete"])
def test_stale_persona_writes_preserve_config(tmp_path: Path, method: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("github_org: acme\ngithub_project_number: 1\nhuman_reviewers: [alice]\ngithub_token: tok\n")
    client = _make_personas_app(tmp_path, config_path=path)
    version = client.get("/api/personas").headers["etag"]
    path.write_text(path.read_text() + "\n# concurrent change\n")
    before = path.read_bytes()
    response = client.request(method, "/api/personas/implementer", json={"instructions": "new"},
                              headers={"If-Match": version})
    assert response.status_code == 409
    assert path.read_bytes() == before


def test_invalid_instructions_do_not_write(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("github_org: acme\ngithub_project_number: 1\nhuman_reviewers: [alice]\ngithub_token: tok\n")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="maximum length"):
        save_persona("implementer", " " + "x" * PERSONA_MAX_LENGTH, path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("method", ["put", "delete"])
def test_persona_write_rechecks_hash_after_reading(tmp_path: Path, method: str) -> None:
    path = tmp_path / "config.yaml"
    client = _make_personas_app(tmp_path)
    version = client.get("/api/personas").headers["etag"]
    concurrent = path.read_text() + "\n# edit during save\n"
    original_load = yaml.safe_load

    def read_then_change(text: str) -> object:
        loaded = original_load(text)
        path.write_text(concurrent)
        return loaded

    with patch.object(yaml, "safe_load", side_effect=read_then_change):
        response = client.request(method, "/api/personas/implementer", json={"instructions": "new"},
                                  headers={"If-Match": version})
    assert response.status_code == 409
    assert response.json()["conflict"] is True
    assert path.read_text() == concurrent


def test_failed_atomic_replace_keeps_original_and_cleans_temp(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    client = _make_personas_app(tmp_path)
    version = client.get("/api/personas").headers["etag"]
    before = path.read_bytes()
    with patch("coordinare.services.config_write_service.os.replace", side_effect=OSError("disk error")):
        response = client.put("/api/personas/implementer", json={"instructions": _TEXT},
                              headers={"If-Match": version})
    assert response.status_code == 500
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_api_rejects_text_that_would_fail_on_reload(tmp_path: Path) -> None:
    client = _make_personas_app(tmp_path)
    path = tmp_path / "config.yaml"
    before = path.read_bytes()
    version = client.get("/api/personas").headers["etag"]
    response = client.put("/api/personas/implementer", json={"instructions": " " + "x" * PERSONA_MAX_LENGTH},
                          headers={"If-Match": version})
    assert response.status_code == 400
    assert path.read_bytes() == before
