"""Unit tests for spec 063 T013/T014 retry loop + rejected manifest writer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from coordinare.services.service_inference import (
    REJECTED_FILENAME,
    InferenceFailed,
    infer_services,
)
from coordinare.services.service_inference.agent import LLMStep
from coordinare.services.service_inference.validator import ValidationResult


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "Gemfile").write_text("source 'https://rubygems.org'\ngem 'rails'\n")
    return proj


@pytest.fixture()
def output_root(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    return out


def _good_manifest() -> dict[str, Any]:
    return {
        "services": [
            {
                "name": "redis",
                "binary": "/usr/bin/redis-server",
                "version": "7.2",
                "data_dir": "/tmp/redis",
                "port": 6379,
                "why_needed": "session store",
                "sources": ["Gemfile"],
            }
        ],
        "cache_inputs": ["Gemfile"],
        "agent_version": "test-1",
    }


def _bad_manifest_missing_required() -> dict[str, Any]:
    # Missing `version`, `data_dir`, etc. → ManifestValidationError from agent.
    return {
        "services": [{"name": "redis"}],
        "cache_inputs": [],
        "agent_version": "test-1",
    }


class _StubClient:
    def __init__(self, script: list[LLMStep]) -> None:
        self._script = list(script)
        self.calls: list[list[dict[str, Any]]] = []

    async def step(self, messages: list[dict[str, Any]]) -> LLMStep:
        self.calls.append(messages)
        return self._script.pop(0)


@pytest.mark.asyncio
async def test_success_on_first_attempt_writes_artifacts(
    project: Path, output_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "coordinare.services.service_inference.validate",
        lambda scripts, **_: ValidationResult(
            phase=None, stdout="ok", stderr="", ok=True, returncode=0
        ),
    )
    client = _StubClient([LLMStep(manifest=_good_manifest())])

    result = await infer_services(
        project_root=project,
        output_root=output_root,
        agent_version="test-1",
        llm_client=client,
        retry_budget=3,
    )

    assert result.attempts == 1
    assert (result.scripts_dir / "services.json").is_file()
    assert (result.scripts_dir / "services-start.sh").is_file()
    assert (result.scripts_dir / "services-stop.sh").is_file()
    assert (result.scripts_dir / "services-health.sh").is_file()
    payload = json.loads((result.scripts_dir / "services.json").read_text())
    assert payload["services"][0]["name"] == "redis"
    # The cache_manifest.txt sidecar lists the manifest's cache_inputs so the
    # outer cache-key composer doesn't have to parse services.json.
    sidecar = result.scripts_dir / "cache_manifest.txt"
    assert sidecar.is_file()
    assert sidecar.read_text().splitlines() == payload["cache_inputs"]


@pytest.mark.asyncio
async def test_validation_failure_then_success_uses_hint(
    project: Path, output_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First validate() fails, second succeeds. Hint must reach second attempt."""
    results = iter(
        [
            ValidationResult(
                phase="health",
                stdout="",
                stderr="port 6379 not listening",
                ok=False,
                returncode=1,
            ),
            ValidationResult(phase=None, stdout="ok", stderr="", ok=True, returncode=0),
        ]
    )
    monkeypatch.setattr(
        "coordinare.services.service_inference.validate",
        lambda scripts, **_: next(results),
    )
    client = _StubClient(
        [LLMStep(manifest=_good_manifest()), LLMStep(manifest=_good_manifest())]
    )

    result = await infer_services(
        project_root=project,
        output_root=output_root,
        agent_version="test-1",
        llm_client=client,
        retry_budget=3,
    )

    assert result.attempts == 2
    # The second call into the LLM should have carried the validator hint.
    second_attempt_first_message = client.calls[1][0]
    assert second_attempt_first_message["role"] == "user"
    assert "port 6379 not listening" in second_attempt_first_message["content"]


@pytest.mark.asyncio
async def test_budget_exhaustion_writes_rejected_and_raises(
    project: Path, output_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "coordinare.services.service_inference.validate",
        lambda scripts, **_: ValidationResult(
            phase="start", stdout="", stderr="boom", ok=False, returncode=42
        ),
    )
    client = _StubClient([LLMStep(manifest=_good_manifest()) for _ in range(2)])

    with pytest.raises(InferenceFailed) as excinfo:
        await infer_services(
            project_root=project,
            output_root=output_root,
            agent_version="test-1",
            llm_client=client,
            retry_budget=2,
        )

    rejected_path = excinfo.value.rejected_path
    assert rejected_path.name == REJECTED_FILENAME
    assert rejected_path.is_file()
    payload = json.loads(rejected_path.read_text())
    assert payload["agent_version"] == "test-1"
    assert payload["manifest"]["services"][0]["name"] == "redis"
    assert "boom" in payload["validation_summary"]
    assert len(payload["attempts"]) == 2
    # Success artifacts must NOT be present.
    assert not (rejected_path.parent / "services.json").exists()


@pytest.mark.asyncio
async def test_agent_error_counted_as_attempt(
    project: Path, output_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema-invalid manifest from the LLM consumes one retry."""
    monkeypatch.setattr(
        "coordinare.services.service_inference.validate",
        lambda scripts, **_: ValidationResult(
            phase=None, stdout="ok", stderr="", ok=True, returncode=0
        ),
    )
    client = _StubClient(
        [
            LLMStep(manifest=_bad_manifest_missing_required()),
            LLMStep(manifest=_good_manifest()),
        ]
    )

    result = await infer_services(
        project_root=project,
        output_root=output_root,
        agent_version="test-1",
        llm_client=client,
        retry_budget=3,
    )
    assert result.attempts == 2


@pytest.mark.asyncio
async def test_retry_budget_must_be_positive(
    project: Path, output_root: Path
) -> None:
    client = _StubClient([])
    with pytest.raises(ValueError):
        await infer_services(
            project_root=project,
            output_root=output_root,
            agent_version="test-1",
            llm_client=client,
            retry_budget=0,
        )


@pytest.mark.asyncio
async def test_skip_validation_writes_artifacts_immediately(
    project: Path, output_root: Path
) -> None:
    """run_validation=False bypasses the validator (used by callers that dry-run elsewhere)."""
    client = _StubClient([LLMStep(manifest=_good_manifest())])
    result = await infer_services(
        project_root=project,
        output_root=output_root,
        agent_version="test-1",
        llm_client=client,
        retry_budget=1,
        run_validation=False,
    )
    assert result.attempts == 1
    assert (result.scripts_dir / "services.json").is_file()


@pytest.mark.asyncio
async def test_stale_rejected_file_removed_on_success(
    project: Path, output_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services_dir = output_root / "services"
    services_dir.mkdir(parents=True)
    stale = services_dir / REJECTED_FILENAME
    stale.write_text("{}")

    monkeypatch.setattr(
        "coordinare.services.service_inference.validate",
        lambda scripts, **_: ValidationResult(
            phase=None, stdout="ok", stderr="", ok=True, returncode=0
        ),
    )
    client = _StubClient([LLMStep(manifest=_good_manifest())])

    await infer_services(
        project_root=project,
        output_root=output_root,
        agent_version="test-1",
        llm_client=client,
        retry_budget=1,
    )

    assert not stale.exists()
