"""#252 env policy: an unset ${NAME} in an environment value is refused only for a secret-looking key.

Any other key is a warning at boot, on save and in ``config validate``, and its entry is left out of
the environment the performer is launched with. The literal ``${NAME}`` is never passed on.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from coordinare import __main__ as daemon_main
from coordinare.config import ProjectConfiguration
from coordinare.dashboard.guards import save_response
from coordinare.env_placeholders import (
    ENV_SECRET_KEY,
    drop_unset_placeholders,
    is_secret_env_key,
    operator_workflow_env,
    unset_placeholder_names,
)
from coordinare.services import kubernetes_runtime, performer_lifecycle
from coordinare.services.config_write_service import (
    SaveResult,
    _validate_candidate,
    candidate_warnings,
)
from coordinare.start_checks import (
    placeholder_problems,
    placeholder_warnings,
    raw_placeholder_warnings,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_UNSET = "COORDINARE_252_ENV_UNSET"
_PLACEHOLDER = f"${{{_UNSET}}}"
_BASE: dict[str, Any] = {
    "github_org": "acme",
    "github_token": "ghp_fixturetoken",
    "human_reviewers": ["alice"],
    "symphonies": [{"name": "demo", "github_project_number": 100}],
}
_SECRET_KEYS = [
    "API_KEY", "COORDINARE_INFERENCE_API_KEY", "LITELLM_MASTER_KEY", "GITHUB_TOKEN", "WEBHOOK_SECRET",
    "DB_PASSWORD", "SMTP_PASS", "SERVICE_CREDENTIAL", "AUTH_HEADER", "api_key", "github_token",
]
_PLAIN_KEYS = [
    "HERMES_MODEL", "HERMES_PROVIDER", "COORDINARE_INFERENCE_MAX_TOKENS", "MAX_TOKENS", "TOKENIZER",
    "COORDINARE_INFERENCE_MAX_TOOL_CALLS", "COORDINARE_INFERENCE_AGENT_VERSION", "LOG_LEVEL", "KEYBOARD_LAYOUT",
]


@pytest.fixture(autouse=True)
def _variable_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_UNSET, raising=False)


def _performer(env: dict[str, str]) -> dict[str, Any]:
    return {
        "id": "remote", "mode": "persistent", "roles": ["implementer"],
        "endpoint": "http://performer.example:8080/", "image": "performer:latest", "env": env,
    }


# --- which keys look secret ---------------------------------------------------------------------


@pytest.mark.parametrize("key", _SECRET_KEYS)
def test_a_secret_looking_key_matches_the_pattern_in_any_case(key: str) -> None:
    assert is_secret_env_key(key)
    assert ENV_SECRET_KEY.search(key) is not None


@pytest.mark.parametrize("key", _PLAIN_KEYS)
def test_a_tuning_key_is_not_secret_because_it_only_contains_a_word(key: str) -> None:
    assert not is_secret_env_key(key)


# --- an unset placeholder: refused for a secret key, a warning for any other --------------------


@pytest.mark.parametrize("key", _SECRET_KEYS)
def test_an_unset_placeholder_under_a_secret_key_is_refused(key: str) -> None:
    problems = placeholder_problems(ProjectConfiguration, {"performer_endpoints": [_performer({key: _PLACEHOLDER})]})
    assert [field for field, _ in problems] == [f"performer_endpoints[0].env.{key}"]
    assert _UNSET in problems[0][1]


@pytest.mark.parametrize("key", _PLAIN_KEYS)
def test_an_unset_placeholder_under_a_plain_key_is_a_warning_and_not_a_refusal(key: str) -> None:
    value = {"performer_endpoints": [_performer({key: _PLACEHOLDER})]}
    assert placeholder_problems(ProjectConfiguration, value) == []
    warnings = placeholder_warnings(ProjectConfiguration, value)
    assert [field for field, _ in warnings] == [f"performer_endpoints[0].env.{key}"]
    assert _UNSET in warnings[0][1]
    assert "left out" in warnings[0][1]


def test_a_plain_key_under_workflow_env_is_a_warning() -> None:
    value = {"performers": {"default": {"workflow_env": {"DOCS_ROOT": _PLACEHOLDER}}}}
    assert placeholder_problems(ProjectConfiguration, value) == []
    assert [field for field, _ in placeholder_warnings(ProjectConfiguration, value)] == [
        "performers.default.workflow_env.DOCS_ROOT",
    ]


def test_a_secret_key_under_workflow_env_is_refused() -> None:
    value = {"performers": {"default": {"workflow_env": {"DOCS_API_KEY": _PLACEHOLDER}}}}
    assert [field for field, _ in placeholder_problems(ProjectConfiguration, value)] == [
        "performers.default.workflow_env.DOCS_API_KEY",
    ]


def test_a_set_placeholder_under_any_key_is_neither_a_refusal_nor_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_UNSET, "set-value")
    value = {"performer_endpoints": [_performer({"HERMES_MODEL": _PLACEHOLDER, "API_KEY": _PLACEHOLDER})]}
    assert placeholder_problems(ProjectConfiguration, value) == []
    assert placeholder_warnings(ProjectConfiguration, value) == []


# --- the save response carries the warning, and is not refused --------------------------------


def test_a_save_with_a_plain_key_placeholder_is_accepted_and_reports_a_warning() -> None:
    raw = {**_BASE, "performer_endpoints": [_performer({"HERMES_MODEL": _PLACEHOLDER})]}
    assert _validate_candidate(raw) == []
    warnings = candidate_warnings(raw)
    assert [w.key for w in warnings] == ["performer_endpoints[0].env.HERMES_MODEL"]
    assert warnings[0].code == "warning"


def test_a_save_with_a_secret_key_placeholder_is_still_refused() -> None:
    raw = {**_BASE, "performer_endpoints": [_performer({"HERMES_API_KEY": _PLACEHOLDER})]}
    errors = _validate_candidate(raw)
    assert [e.key for e in errors] == ["performer_endpoints[0].env.HERMES_API_KEY"]


def test_the_save_response_carries_the_warning_and_is_ok() -> None:
    """The response body the save routes return: ok, with the warning beside the (empty) errors."""
    raw = {**_BASE, "performer_endpoints": [_performer({"HERMES_MODEL": _PLACEHOLDER})]}
    response = save_response(SaveResult(ok=True, warnings=candidate_warnings(raw)), 200)
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["errors"] == []
    assert [w["key"] for w in body["warnings"]] == ["performer_endpoints[0].env.HERMES_MODEL"]


# --- what is left out of the launched environment -----------------------------------------------


def test_the_launch_leaves_out_an_unset_entry_and_passes_the_rest() -> None:
    kept, dropped = drop_unset_placeholders({"HERMES_MODEL": _PLACEHOLDER, "BACKEND": "opencode", "X": f"a-${{{_UNSET}}}"})
    assert kept == {"BACKEND": "opencode"}
    assert dropped == ["HERMES_MODEL", "X"]


def test_unset_placeholder_names_lists_only_the_unset_names() -> None:
    assert unset_placeholder_names(f"{_PLACEHOLDER}-${{PATH}}") == [_UNSET]
    assert unset_placeholder_names("plain") == []


def test_the_workflow_env_a_role_passes_on_has_no_unset_placeholder() -> None:
    role = SimpleNamespace(workflow_env={"DOCS_ROOT": _PLACEHOLDER, "KEEP": "1"})
    assert operator_workflow_env(role) == {"KEEP": "1"}


def test_the_docker_and_kubernetes_launch_paths_use_the_same_drop() -> None:
    docker = inspect.getsource(performer_lifecycle.start_ephemeral)
    kubernetes = inspect.getsource(kubernetes_runtime)
    assert "drop_unset_placeholders" in docker
    assert "drop_unset_placeholders" in kubernetes


# --- boot and config validate report the same findings ------------------------------------------


def test_the_boot_check_lets_a_plain_placeholder_through_and_returns_it_as_a_warning(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({**_BASE, "performer_endpoints": [_performer({"HERMES_MODEL": _PLACEHOLDER})]}))
    unexpanded = daemon_main._check_placeholders_as_written(path)
    assert unexpanded["github_org"] == "acme"
    assert [field for field, _ in raw_placeholder_warnings(unexpanded)] == ["performer_endpoints[0].env.HERMES_MODEL"]
    assert [field for field, _ in daemon_main._warn_unset_placeholders(unexpanded)] == [
        "performer_endpoints[0].env.HERMES_MODEL",
    ]


def _daemon_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("COORDINARE_")}
    packages = ["src", "agent/performer/src", "packages/ci_detection/src", "packages/service_inference/src",
                "packages/routing_models/src"]
    env["PYTHONPATH"] = os.pathsep.join(str(_REPO_ROOT / p) for p in packages)
    env.pop(_UNSET, None)
    return env


def _validate(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({**_BASE, "performer_endpoints": [_performer(env)]}, sort_keys=False))
    return subprocess.run(
        [sys.executable, "-m", "coordinare", "config", "validate", "--config", str(path)],
        cwd=tmp_path, env=_daemon_env(), capture_output=True, text=True, timeout=120, check=False,
    )


def test_config_validate_reports_a_secret_refusal_and_exits_one(tmp_path: Path) -> None:
    result = _validate(tmp_path, {"HERMES_API_KEY": _PLACEHOLDER})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "performer_endpoints[0].env.HERMES_API_KEY" in result.stdout
    assert _UNSET in result.stdout


def test_config_validate_reports_a_plain_warning_and_exits_zero(tmp_path: Path) -> None:
    result = _validate(tmp_path, {"HERMES_MODEL": _PLACEHOLDER})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "performer_endpoints[0].env.HERMES_MODEL" in result.stdout
    assert "left out" in result.stdout


def test_config_validate_strict_treats_the_plain_warning_as_a_failure(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({**_BASE, "performer_endpoints": [_performer({"HERMES_MODEL": _PLACEHOLDER})]}))
    result = subprocess.run(
        [sys.executable, "-m", "coordinare", "config", "validate", "--config", str(path), "--strict"],
        cwd=tmp_path, env=_daemon_env(), capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
