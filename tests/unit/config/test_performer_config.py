"""Tests for performer_endpoints YAML loading (spec 056, T007)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinare.config import ProjectConfiguration


def _write(tmp_path: Path, payload: dict[str, object]) -> Path:
    base: dict[str, object] = {
        "project_name": "test",
        "github_org": "owner",
        "github_repo": "owner/repo",
        "github_project_number": 1,
        "github_auth": "pat",
        "github_token": "ghp_test",
        "human_reviewers": ["alice"],
    }
    base.update(payload)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(base))
    return p


def test_default_performer_endpoints_is_empty(tmp_path: Path) -> None:
    cfg = ProjectConfiguration.from_yaml(_write(tmp_path, {}))
    assert cfg.performer_endpoints == []


def test_loads_subprocess_entry_with_default_mode(tmp_path: Path) -> None:
    cfg = ProjectConfiguration.from_yaml(
        _write(
            tmp_path,
            {"performer_endpoints": [{"id": "p1", "roles": ["implementer"]}]},
        ),
    )
    assert len(cfg.performer_endpoints) == 1
    assert cfg.performer_endpoints[0].mode == "subprocess"


def test_loads_persistent_entry(tmp_path: Path) -> None:
    cfg = ProjectConfiguration.from_yaml(
        _write(
            tmp_path,
            {
                "performer_endpoints": [
                    {
                        "id": "claude1",
                        "mode": "persistent",
                        "roles": ["implementer"],
                        "image": "performer:full",
                        "endpoint": "http://localhost:8081",
                    },
                ],
            },
        ),
    )
    assert cfg.performer_endpoints[0].mode == "persistent"
    assert cfg.performer_endpoints[0].image == "performer:full"


def test_unknown_field_in_endpoint_rejected(tmp_path: Path) -> None:
    with pytest.raises(Exception, match=r"Extra inputs|extra_forbidden|unknown"):
        ProjectConfiguration.from_yaml(
            _write(
                tmp_path,
                {
                    "performer_endpoints": [
                        {"id": "p1", "roles": ["impl"], "garbage": True},
                    ],
                },
            ),
        )


def test_duplicate_id_rejected(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="duplicate id"):
        ProjectConfiguration.from_yaml(
            _write(
                tmp_path,
                {
                    "performer_endpoints": [
                        {"id": "p1", "roles": ["a"]},
                        {"id": "p1", "roles": ["b"]},
                    ],
                },
            ),
        )


def test_duplicate_endpoint_url_rejected(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="duplicate endpoint URL"):
        ProjectConfiguration.from_yaml(
            _write(
                tmp_path,
                {
                    "performer_endpoints": [
                        {
                            "id": "a",
                            "mode": "persistent",
                            "roles": ["x"],
                            "image": "i",
                            "endpoint": "http://localhost:9000",
                        },
                        {
                            "id": "b",
                            "mode": "persistent",
                            "roles": ["y"],
                            "image": "i",
                            "endpoint": "http://localhost:9000",
                        },
                    ],
                },
            ),
        )
