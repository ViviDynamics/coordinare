from __future__ import annotations

import os
import shlex
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Any, NoReturn

import yaml

from coordinare.presets import HARNESS_DEFAULTS
from coordinare.user_config import (
    ENV_FILENAME,
    GENERATED_HEADER,
    PRIVATE_DIRECTORY_MODE,
    SECRET_MODE,
    user_data_directory,
)

REFERENCE = "https://github.com/ViviDynamics/coordinare/blob/main/config.example.yaml"


class SetupError(ValueError):
    """A deliberately credential-free error that can be shown to the operator."""


def fail(message: str) -> NoReturn:
    raise SetupError(message)


def read_mapping(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text())
    if not isinstance(value, dict):
        fail("Expected a YAML mapping of setup answers or configuration fields.")
    if any(not isinstance(key, str) for key in value):
        fail("Setup answers and configuration fields must have text keys.")
    return dict(value)


def minimal_template() -> dict[str, Any]:
    resource = files("coordinare").joinpath("data/config.minimal.yaml")
    if resource.is_file():
        return dict(yaml.safe_load(resource.read_text(encoding="utf-8")))
    # Source checkouts use the same file that the wheel includes.
    return read_mapping(Path(__file__).resolve().parents[2] / "config.minimal.yaml")


def build_config(answers: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    raw = {**minimal_template(), **existing}
    owner, repo = answers["repository"].split("/")
    data = user_data_directory()
    raw.update({
        "preset": "containers" if answers["run_mode"] == "docker" else "solo",
        "harness": answers["harness"], "github_org": owner, "github_repo": repo,
        "github_project_number": answers["project_number"],
        "github_token": "${COORDINARE_GITHUB_TOKEN}",
        "human_reviewers": answers["human_reviewers"],
        "max_concurrent_cards": answers["max_concurrent_cards"],
    })
    raw.setdefault("state_file_path", str(data / "state.json"))
    raw.setdefault("run_logs", {"dir": str(data / "run-logs")})
    raw.setdefault("transcripts", {"dir": str(data / "transcripts")})
    configure_model(raw, answers)
    return raw


def configure_model(raw: dict[str, Any], answers: dict[str, Any]) -> None:
    kind, credential, default_model = HARNESS_DEFAULTS[answers["harness"]]
    if "endpoints" in raw or answers.get("endpoint") or not answers.get("api_key"):
        previous: dict[str, Any] = next((entry for entry in raw.get("endpoints", []) if entry.get("name") == "starter"), {})
        if answers.get("endpoint") and previous.get("base_url") == answers["endpoint"]:
            kind = previous.get("kind", "litellm")
        elif answers.get("endpoint"):
            kind = "litellm"
        endpoint: dict[str, Any] = {
            "name": "starter", "kind": kind,
            "auth_env": credential if answers.get("api_key") else None,
            "base_url": answers.get("endpoint") or None,
        }
        raw["endpoints"] = _replace_starter(raw.get("endpoints", []), endpoint)
    if "model_endpoints" in raw or answers["model"] != default_model:
        model = {"name": "starter", "endpoint": "starter", "model": answers["model"]}
        raw["model_endpoints"] = _replace_starter(raw.get("model_endpoints", []), model)


def _replace_starter(entries: list[dict[str, Any]], replacement: dict[str, Any]) -> list[dict[str, Any]]:
    previous = next((entry for entry in entries if entry.get("name") == "starter"), {})
    return [{**previous, **replacement}, *(entry for entry in entries if entry.get("name") != "starter")]


def secret_values(answers: dict[str, Any], previous: dict[str, str]) -> dict[str, str]:
    values = {**previous, "COORDINARE_GITHUB_TOKEN": answers["github_token"]}
    credential = HARNESS_DEFAULTS[answers["harness"]][1]
    if answers.get("api_key"):
        values[credential] = answers["api_key"]
    else:
        values.pop(credential, None)
    if any(any(char in value for char in "\n\r\0") for value in values.values()):
        fail("Secrets must be single-line values without NUL characters.")
    return values


def _backup(path: Path) -> None:
    if not path.exists():
        return
    target = path.with_name(path.name + ".bak")
    while target.exists():
        target = target.with_name(target.name + ".bak")
    _write_atomic(target, path.read_text(), replace=False)


def _write_atomic(path: Path, content: str, *, replace: bool) -> None:
    staging = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
            staging = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        staging.chmod(SECRET_MODE)
        if replace:
            staging.replace(path)
        else:
            path.hardlink_to(staging)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


def write_setup(directory: Path, raw: dict[str, Any], secrets: dict[str, str], *, force: bool) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    config_path = directory / "config.yaml"
    env_path = directory / ENV_FILENAME
    paths = (config_path, env_path)
    if any(path.is_symlink() for path in paths):
        fail("Setup files cannot be symbolic links. Choose another --config-dir.")
    if not force and any(path.exists() for path in paths):
        fail("Setup files already exist. Use --force to keep backups and replace them.")
    for path in paths:
        _backup(path)
    env = "".join(f"{key}={shlex.quote(value)}\n" for key, value in secrets.items())
    text = f"{GENERATED_HEADER} Full reference: {REFERENCE}\n" + yaml.safe_dump(raw, sort_keys=False)
    original_env = env_path.read_text() if env_path.exists() else None
    _write_atomic(env_path, env, replace=force)
    try:
        _write_atomic(config_path, text, replace=force)
    except OSError:
        if original_env is None:
            env_path.unlink(missing_ok=True)
        else:
            _write_atomic(env_path, original_env, replace=True)
        raise
    return config_path
