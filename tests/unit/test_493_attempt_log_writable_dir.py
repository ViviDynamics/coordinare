"""Regression tests for issue 493 — attempt log anchored to an unwritable config dir.

In containerized deployments config.yaml is mounted under a timestamped
read-only ConfigMap symlink directory (/etc/coordinare/<ts>/). The attempt log
anchored there and every append failed with EROFS, losing all attempt history.
Resolution must fall back to the writable state dir.
"""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest
import structlog.testing

import coordinare.__main__ as app_main
from coordinare.attempt_log import AttemptLog, resolve_writable_log_dir
from coordinare.config import PerformerRoleConfig, PerformersConfig, ProjectConfiguration

requires_nonroot = pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 1)() == 0,
    reason="a root user bypasses permission bits, so the read-only probe would not fail",
)


@requires_nonroot
def test_resolve_returns_candidate_when_writable(tmp_path: Path) -> None:
    """Bare-metal path: a writable candidate is used unchanged."""
    candidate = tmp_path / "logs" / "attempts"
    assert resolve_writable_log_dir(candidate, tmp_path / "state") == candidate


@requires_nonroot
def test_resolve_falls_back_when_config_dir_is_readonly(tmp_path) -> None:
    """The k8s ConfigMap case: the config anchor's parent is read-only."""
    config_dir = tmp_path / "etc" / "coordinare" / "..2026_09_27_15_57_13.3348541120"
    config_dir.mkdir(parents=True)
    candidate = config_dir / "logs" / "attempts"
    fallback = tmp_path / "state" / "logs" / "attempts"
    config_dir.chmod(0o555)
    try:
        with structlog.testing.capture_logs() as captured:
            resolved = resolve_writable_log_dir(candidate, fallback)
    finally:
        config_dir.chmod(0o755)
    assert resolved == fallback
    assert any(
        r["event"] == "attempt_log.dir_unwritable" and r["log_level"] == "warning"
        for r in captured
    )


@requires_nonroot
def test_resolve_falls_back_when_candidate_exists_but_is_readonly(tmp_path) -> None:
    """The candidate directory exists but the filesystem rejects writes (EROFS analogue)."""
    candidate = tmp_path / "attempts"
    candidate.mkdir()
    candidate.chmod(0o555)
    try:
        resolved = resolve_writable_log_dir(candidate, tmp_path / "state")
    finally:
        candidate.chmod(0o755)
    assert resolved == tmp_path / "state"


@requires_nonroot
def test_resolve_returns_fallback_when_candidate_equals_fallback_and_unwritable(tmp_path) -> None:
    """No config path: candidate IS the state anchor. Unwritable there is still best-effort."""
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o555)
    try:
        anchor = state / "logs" / "attempts"
        resolved = resolve_writable_log_dir(anchor, anchor)
    finally:
        state.chmod(0o755)
    assert resolved == state / "logs" / "attempts"


@requires_nonroot
def test_attempt_row_is_written_to_state_fallback_when_config_dir_readonly(tmp_path) -> None:
    """End-to-end repro: the attempt row lands on the writable state dir, not the RO anchor."""
    config_dir = tmp_path / "etc" / "coordinare" / "..2026_09_27_15_57_13.3348541120"
    config_dir.mkdir(parents=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    candidate = config_dir / "logs" / "attempts"
    fallback = state_dir / "logs" / "attempts"

    config_dir.chmod(0o555)
    try:
        log = AttemptLog(log_dir=resolve_writable_log_dir(candidate, fallback))
        log.open_attempt(task_id="card-1", routing_reason="default_policy")
    finally:
        config_dir.chmod(0o755)

    files = sorted(fallback.glob("*.jsonl"))
    assert files, "attempt history lost: no JSONL written to the writable fallback"
    row = json.loads(files[0].read_text().splitlines()[0])
    assert row["task_id"] == "card-1"
    assert not candidate.exists()


def _config(state_file_path: Path, performer_log_dir: Path | None = None) -> ProjectConfiguration:
    return ProjectConfiguration(
        project_name="test-project",
        github_org="acme",
        github_project_number=1,
        github_token="token",
        human_reviewers=["alice"],
        performers=PerformersConfig(default=PerformerRoleConfig(backend="codex", effort="high")),
        state_file_path=state_file_path,
        performer_log_dir=performer_log_dir,
    )


@requires_nonroot
def test_bootstrap_resolution_writes_attempt_row_to_state_dir(tmp_path) -> None:
    """Wiring gap (copilot review): the bootstrap must resolve through the fallback,
    not just the helper in isolation. Read-only config anchor + writable state dir
    must produce an attempt row under the state dir.
    """
    config_dir = tmp_path / "etc" / "coordinare" / "..2026_09_27_15_57_13.3348541120"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text("placeholder\n")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = _config(state_file_path=state_dir / "coordinare.state.json")

    config_dir.chmod(0o555)
    try:
        log_dir = app_main._resolve_attempt_log_dir(config, config_path)
        attempt_log = AttemptLog(log_dir=log_dir)
        attempt_log.open_attempt(task_id="card-1", routing_reason="default_policy")
    finally:
        config_dir.chmod(0o755)

    files = sorted((state_dir / "logs" / "attempts").glob("*.jsonl"))
    assert files, "attempt history lost: bootstrap resolution did not use the writable state dir"
    row = json.loads(files[0].read_text().splitlines()[0])
    assert row["task_id"] == "card-1"
    assert not (config_dir / "logs").exists()


@requires_nonroot
def test_bootstrap_resolution_honours_performer_log_dir(tmp_path) -> None:
    """A configured performer_log_dir still wins, and is subject to the same fallback."""
    ro_base = tmp_path / "ro-logs"
    ro_base.mkdir()
    ro_base.chmod(0o555)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = _config(
        state_file_path=state_dir / "coordinare.state.json",
        performer_log_dir=ro_base / "performer-logs",
    )
    try:
        log_dir = app_main._resolve_attempt_log_dir(config, None)
        attempt_log = AttemptLog(log_dir=log_dir)
        attempt_log.open_attempt(task_id="card-2", routing_reason="default_policy")
    finally:
        ro_base.chmod(0o755)

    files = sorted((state_dir / "logs" / "attempts").glob("*.jsonl"))
    assert files, "attempt history lost: fallback did not engage for the configured dir"
    assert not (ro_base / "attempts").exists()


def test_bootstrap_services_wires_the_resolver() -> None:
    """Mechanical wiring check (138-style source assertion): removing the resolver
    call from _bootstrap_services would otherwise leave every functional test green
    while restoring the deployed failure.
    """
    source = inspect.getsource(app_main._bootstrap_services)
    assert "_resolve_attempt_log_dir(config, config_path)" in source
