"""Spec 219, final narrow check: Unicode spellings, validate-what-loads, fail-closed, re-check.

Four findings. NFC and NFD spellings of one name are the same directory on APFS, so the overlap
rule and the runtime walk must compare normalised components. The dashboard saves must validate
the bytes the daemon will load, which means after ``os.path.expandvars``. A case probe that cannot
tell must fail closed. And the global save must re-check its concurrency guard just before the
write, the same as the section save does.
"""

from __future__ import annotations

import copy
import os
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

import pydantic
import pytest
import yaml

import coordinare.dashboard.routers.config as dashboard_config_routes
from coordinare.config import ProjectConfiguration
from coordinare.models.env_cache import EnvCacheState
from coordinare.models.performer_endpoint import PerformerEndpointConfig, VolumeMount
from coordinare.services import env_cache
from coordinare.services.config_write_service import (
    SaveRequest,
    compute_content_hash,
    save_section,
)
from coordinare.services.mounted_file import _parts_below
from coordinare.services.run_transcript import _case_insensitive, _within
from tests.unit.conftest import REPRESENTATIVE_CONFIG
from tests.unit.test_219_case_and_candidate import _http_client, _raw

NFC = unicodedata.normalize("NFC", "café")
NFD = unicodedata.normalize("NFD", "café")
_BASE: dict[str, Any] = {
    "project_name": "Demo",
    "github_org": "acme",
    "github_project_number": 1,
    "github_token": "tok",
    "human_reviewers": ["alice"],
}


@pytest.fixture(autouse=True)
def _github_token_in_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixtures keep ${COORDINARE_GITHUB_TOKEN} in the file; the daemon reads it from the environment (225)."""
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "ghp_realtoken")


def _endpoint(volume: Path) -> PerformerEndpointConfig:
    return PerformerEndpointConfig(
        id="p1",
        mode="ephemeral",
        image="performer:full",
        roles=["implementer"],
        volumes=[VolumeMount(host_path=volume, container_path=PurePosixPath("/work"), mode="rw")],
    )


# ---------------------------------------------------------------------------
# BLOCK 1: NFC and NFD spellings of one name are one name.
# ---------------------------------------------------------------------------


def test_nfd_root_under_an_nfc_volume_is_refused_at_load(tmp_path: Path) -> None:
    volume = tmp_path / NFC
    with pytest.raises(pydantic.ValidationError, match="overlaps"):
        ProjectConfiguration(
            **_BASE,
            env_cache_root=tmp_path / NFD / "caches",
            performer_endpoints=[_endpoint(volume)],
        )


def test_nfc_root_under_an_nfd_volume_is_refused_at_load(tmp_path: Path) -> None:
    volume = tmp_path / NFD
    with pytest.raises(pydantic.ValidationError, match="overlaps"):
        ProjectConfiguration(
            **_BASE,
            env_cache_root=tmp_path / NFC / "caches",
            performer_endpoints=[_endpoint(volume)],
        )


def test_within_treats_nfc_and_nfd_as_one_name(tmp_path: Path) -> None:
    assert _within(tmp_path / NFD / "x", tmp_path / NFC)
    assert _within(tmp_path / NFC, tmp_path / NFD)


def test_the_runtime_walk_treats_nfc_and_nfd_as_one_name(tmp_path: Path) -> None:
    """Pure component comparison, independent of what this filesystem does with the bytes."""
    assert _parts_below(tmp_path / NFD / "caches" / "sym", tmp_path / NFC) == ("caches", "sym")


def test_a_planted_root_link_under_nfc_volume_is_refused_through_an_nfd_root(
    tmp_path: Path,
) -> None:
    """The performer replaces the root entry with a link to a host directory. The root is
    spelled NFD; the volume is NFC. Both spellings name one directory on APFS, and the walk
    must treat them as one, so the link is refused. The check does not depend on the filesystem.
    """
    ws = tmp_path / NFC
    ws.mkdir()
    victim = tmp_path / "victim" / "sym"
    victim.mkdir(parents=True)
    (victim / "activate.sh").write_text("HOST\n")
    os.symlink(victim.parent, ws / "caches")
    root_nfd = tmp_path / NFD / "caches"
    state = EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir=root_nfd / "sym")

    mount = env_cache.get_env_volume_for_symphony(
        "sym",
        {"sym": state},
        is_bootstrap=True,
        performer_mounts=(ws,),
    )

    assert mount is None
    assert sorted(p.name for p in victim.iterdir()) == ["activate.sh"]


def test_dashboard_save_of_an_nfd_root_under_an_nfc_volume_is_refused(tmp_path: Path) -> None:
    volume = tmp_path / NFC
    volume.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(_raw(tmp_path / "env-caches", volume)))
    client = _http_client(config_path)
    before = config_path.read_bytes()
    etag = client.get("/api/config/global").headers["etag"]

    resp = client.put(
        "/api/config/global",
        json={"env_cache_root": str(tmp_path / NFD / "caches"), "expected_hash": etag},
    )

    assert resp.status_code == 422
    assert config_path.read_bytes() == before


def _section_config(path: Path, volume: Path) -> None:
    """A config valid for the section write path, with one performer endpoint on ``volume``."""
    raw = copy.deepcopy(REPRESENTATIVE_CONFIG)
    raw["env_cache_root"] = str(path.parent / "env-caches")
    raw["performer_endpoints"] = [
        {
            "id": "p1",
            "mode": "ephemeral",
            "image": "performer:full",
            "roles": ["implementer"],
            "volumes": [{"host_path": str(volume), "container_path": "/work", "mode": "rw"}],
        },
    ]
    path.write_text(yaml.safe_dump(raw, sort_keys=False))


def _section_save(config_path: Path, root: str) -> Any:
    return save_section(
        SaveRequest(
            store="config_yaml",
            section="global",
            changes={"env_cache_root": root},
            base_hash=compute_content_hash(config_path),
        ),
        config_path,
    )


def test_section_save_of_an_nfd_root_under_an_nfc_volume_is_refused(tmp_path: Path) -> None:
    volume = tmp_path / NFC
    volume.mkdir()
    config_path = tmp_path / "config.yaml"
    _section_config(config_path, volume)
    assert _section_save(config_path, str(tmp_path / "elsewhere")).ok is True, (
        "the baseline save must succeed, or the refusal below proves nothing"
    )
    before = config_path.read_bytes()

    result = _section_save(config_path, str(tmp_path / NFD / "caches"))

    assert result.ok is False
    assert config_path.read_bytes() == before


# ---------------------------------------------------------------------------
# BLOCK 2: validate exactly what the daemon loads, after os.path.expandvars.
# ---------------------------------------------------------------------------


def test_dashboard_save_is_validated_after_env_expansion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A volume ``host_path: ${WSDIR}`` expands to a directory the new root lies inside."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("WSDIR", str(ws))
    raw = _raw(tmp_path / "env-caches", tmp_path / "placeholder")
    raw["performer_endpoints"][0]["volumes"][0]["host_path"] = "${WSDIR}"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    client = _http_client(config_path)
    before = config_path.read_bytes()
    etag = client.get("/api/config/global").headers["etag"]

    resp = client.put(
        "/api/config/global",
        json={"env_cache_root": str(ws / "caches"), "expected_hash": etag},
    )

    assert resp.status_code == 422, "the saved file would not load on the daemon"
    assert config_path.read_bytes() == before
    # The literal text is what gets written when a save is allowed, so the placeholder stays.
    ok = client.put(
        "/api/config/global",
        json={"env_cache_root": str(tmp_path / "other"), "expected_hash": etag},
    )
    assert ok.status_code == 200
    assert "${WSDIR}" in config_path.read_text()


def test_section_save_is_validated_after_env_expansion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("WSDIR", str(ws))
    config_path = tmp_path / "config.yaml"
    _section_config(config_path, tmp_path / "placeholder")
    raw = yaml.safe_load(config_path.read_text())
    raw["performer_endpoints"][0]["volumes"][0]["host_path"] = "${WSDIR}"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    assert _section_save(config_path, str(tmp_path / "elsewhere")).ok is True
    before = config_path.read_bytes()

    result = _section_save(config_path, str(ws / "caches"))

    assert result.ok is False
    assert config_path.read_bytes() == before


# ---------------------------------------------------------------------------
# Fail closed: a probe that cannot tell treats the filesystem as case-insensitive.
# ---------------------------------------------------------------------------


def test_case_probe_fails_closed_when_the_name_has_no_letters(tmp_path: Path) -> None:
    digits = tmp_path / "123"
    digits.mkdir()

    assert _case_insensitive(digits) is True


def test_case_probe_fails_closed_at_the_filesystem_root() -> None:
    assert _case_insensitive(Path("/")) is True


# ---------------------------------------------------------------------------
# The global save re-checks its concurrency guard right before the write.
# ---------------------------------------------------------------------------


def test_a_colleague_edit_during_global_validation_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(_raw(tmp_path / "env-caches", tmp_path / "junie")))
    client = _http_client(config_path)
    etag = client.get("/api/config/global").headers["etag"]
    real_validate = dashboard_config_routes._validate_global_config

    def colleague_saves_first(loaded: dict[str, Any], **kwargs: Any) -> Any:
        # Runs after the guard and the read, before the write: a colleague's save lands here.
        config_path.write_text(config_path.read_text() + "\n# a colleague's edit\n")
        return real_validate(loaded, **kwargs)

    monkeypatch.setattr(dashboard_config_routes, "_validate_global_config", colleague_saves_first)

    resp = client.put(
        "/api/config/global",
        json={"max_concurrent_cards": 4, "expected_hash": etag},
    )

    assert resp.status_code == 409
    assert "# a colleague's edit" in config_path.read_text()
