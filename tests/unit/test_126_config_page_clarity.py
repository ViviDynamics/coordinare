"""#126: the Config page says which value is in effect and speaks plainly.

It showed "Config version 0 · routing unavailable" and a global
``github_project_number`` of 0 while the symphonies actually used projects
#5 and #16, with nothing to say which value applied.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from coordinare import config_descriptors as cd
from coordinare.config import CoordinareConfiguration, ProjectConfiguration, SymphonyConfig
from coordinare.dashboard import _DASHBOARD_JS_SOURCES

_CONFIG_JS = _DASHBOARD_JS_SOURCES["config"]
_HELPERS_JS = _DASHBOARD_JS_SOURCES["helpers"]
_CHECKS = Path(__file__).resolve().parents[1] / "js" / "config_clarity_checks.js"


def _config(*symphonies: SymphonyConfig) -> CoordinareConfiguration:
    return CoordinareConfiguration(
        global_config=ProjectConfiguration(
            project_name="Demo", github_org="acme", github_project_number=1,
            github_token="tok", human_reviewers=["alice"],
        ),
        symphonies=list(symphonies),
    )


def test_every_symphony_is_listed_against_the_project_number() -> None:
    overrides = cd.symphony_overrides(_config(
        SymphonyConfig(name="placeholder", github_project_number=5, enabled=False),
        SymphonyConfig(name="conductor-e2e", github_project_number=16),
    ))
    assert [(o.symphony, o.value) for o in overrides["github_project_number"]] == [
        ("placeholder", "#5"), ("conductor-e2e", "#16"),
    ]


def test_fields_under_overrides_are_listed_by_name_without_their_values() -> None:
    """An override value can be a secret, so only the symphony name is returned."""
    overrides = cd.symphony_overrides(_config(
        SymphonyConfig(name="a", github_project_number=2,
                       overrides={"project_name": "sample", "github_token": "ghp_secretsecretsecret"}),
        SymphonyConfig(name="b", github_project_number=3, overrides={"project_name": "other"}),
    ))
    assert [(o.symphony, o.value) for o in overrides["project_name"]] == [("a", None), ("b", None)]
    assert [(o.symphony, o.value) for o in overrides["github_token"]] == [("a", None)]
    dumped = repr({key: [o.model_dump() for o in value] for key, value in overrides.items()})
    assert "ghp_secretsecretsecret" not in dumped and "sample" not in dumped


def test_no_symphonies_means_no_overrides() -> None:
    assert cd.symphony_overrides(SimpleNamespace(symphonies=[])) == {}
    assert cd.symphony_overrides(None) == {}
    assert cd.symphony_overrides(SimpleNamespace(symphonies=None)) == {}


def test_odd_symphony_shapes_never_raise() -> None:
    config = SimpleNamespace(symphonies=[
        SimpleNamespace(name="", github_project_number=9),
        SimpleNamespace(name="x", github_project_number=True, overrides="not a dict"),
        SimpleNamespace(name="y", github_project_number=None, overrides={"github_project_number": 4, "priority": {}}),
    ])
    overrides = cd.symphony_overrides(config)
    assert "github_project_number" not in overrides
    assert [o.symphony for o in overrides["priority"]] == ["y"]


def test_snapshot_carries_overrides_and_the_routing_reason() -> None:
    config = _config(SymphonyConfig(name="conductor-e2e", github_project_number=16))
    snapshot = cd.build_snapshot(config, routing_available=False, routing_mounted=True).model_dump(mode="json")
    assert snapshot["routing_mounted"] is True and snapshot["routing_available"] is False
    assert snapshot["symphony_overrides"] == {
        "github_project_number": [{"symphony": "conductor-e2e", "value": "#16"}],
    }
    assert cd.build_snapshot(config).model_dump(mode="json")["routing_mounted"] is False


def test_the_old_status_text_is_gone_from_the_page_script() -> None:
    assert "'Config version '" not in _CONFIG_JS
    assert "routing unavailable" not in _CONFIG_JS
    assert "cfgStatusLine(data)" in _CONFIG_JS
    # Only the global group asks for override notes; catalog items never do.
    assert _CONFIG_JS.count("cfgFieldRow(s, true)") == 1


def _fn(source: str, name: str) -> str:
    match = re.search(rf"function {name}\(.*?\n\}}", source, re.DOTALL)
    assert match is not None, f"{name} is no longer in the shipped JS"
    return match.group(0)


def test_config_clarity_client_js(tmp_path: Path) -> None:
    """Runs under node, which the project already requires (spec 124)."""
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "config.js"
    js_path.write_text(
        "\n\n".join([_fn(_HELPERS_JS, "esc"), _fn(_CONFIG_JS, "cfgOverrideNote"), _fn(_CONFIG_JS, "cfgStatusLine")]),
        encoding="utf-8",
    )
    result = subprocess.run([node, str(_CHECKS), str(js_path)], capture_output=True, text=True, timeout=60, check=False)
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 20, f"only {summary.group(1)} checks ran:\n{report}"
