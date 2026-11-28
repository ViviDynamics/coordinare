"""451: a role a stage provider fills runs no local harness and no performer.

``performers.<role>.provider`` hands the role to a stage provider (QAre for qa, Scrutare
for reviewer, Progettare for architect), whose own doctor checks run in
``check_stage_providers``. The local harness and performer protocol checks apply only to
roles that dispatch to a performer, so a provider-only config passes them, a mixed config
checks only its performer roles, and a role with neither still fails.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coordinare import launch_preflight
from coordinare.config import ProjectConfiguration
from coordinare.doctor import CheckResult, Status

_PROVIDERS: dict[str, dict[str, Any]] = {
    "qare": {"kind": "qare", "version": "2026.11.44", "image": "coordinare-performer:sandbox"},
    "scrutare": {
        "kind": "scrutare", "url": "http://scrutare.test", "token_env": "SCRUTARE_API_TOKEN",
        "installation": 7, "review_login": "vivi-scrutare[bot]",
    },
    "progettare": {
        "kind": "progettare", "version": "2026.11.3", "runs_dir": "/var/lib/coordinare/progettare-runs",
        "gh_token_env": "GITHUB_TOKEN",
    },
}
_ROLE_OF = {"qare": "qa", "scrutare": "reviewer", "progettare": "architect"}


def _config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, performers: dict[str, Any], **extra: Any,
) -> ProjectConfiguration:
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    return ProjectConfiguration.model_validate({
        "project_name": "widgets", "github_org": "acme", "github_repo": "widgets",
        "github_project_number": 1, "github_token": "token", "human_reviewers": ["alice"],
        "stage_providers": _PROVIDERS, "performers": performers, **extra,
    })


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    """Every harness is missing from PATH; every command probe is recorded and answers."""
    calls: list[tuple[str, list[str]]] = []

    def probe(name: str, command: list[str], fix: str, options: Any = None) -> CheckResult:
        calls.append((name, command))
        return CheckResult(name, Status.OK, f"{name} answered.")

    monkeypatch.setattr(launch_preflight.shutil, "which", lambda _name: None)
    monkeypatch.setattr(launch_preflight, "_command_probe", probe)
    return calls


def _names(results: list[CheckResult]) -> list[str]:
    return [result.name for result in results]


def _failed(results: list[CheckResult]) -> list[str]:
    return [result.name for result in results if result.status is Status.FAIL]


@pytest.mark.parametrize("provider", sorted(_PROVIDERS))
async def test_a_provider_only_config_runs_no_local_harness_or_performer_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, list[str]]], provider: str,
) -> None:
    config = _config(tmp_path, monkeypatch, {_ROLE_OF[provider]: {"provider": provider}})

    results = await launch_preflight.check_harnesses(config)

    assert _failed(results) == []
    assert not any(name.startswith("harness ") for name in _names(results))
    assert "performer protocol" not in _names(results)
    assert "performer transport" not in _names(results)
    assert probes == []


async def test_every_canonical_provider_role_at_once_needs_no_performer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, list[str]]],
) -> None:
    performers = {role: {"provider": provider} for provider, role in _ROLE_OF.items()}
    config = _config(tmp_path, monkeypatch, performers, agent_executable="/opt/performer/bin/run")

    results = await launch_preflight.check_harnesses(config)

    assert _failed(results) == []
    assert probes == []


async def test_a_mixed_config_checks_only_its_performer_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, list[str]]],
) -> None:
    config = _config(
        tmp_path, monkeypatch,
        {"implementer": {"backend": "codex"}, "qa": {"provider": "qare"}, "reviewer": {"provider": "scrutare"}},
        agent_executable="/opt/performer/bin/run",
    )

    results = await launch_preflight.check_harnesses(config)

    # The implementer's codex harness is checked; the provider roles add no nare harness.
    assert [name for name in _names(results) if name.startswith("harness ")] == ["harness codex"]
    assert probes == [("performer protocol", ["/opt/performer/bin/run"])]


async def test_a_role_with_neither_a_provider_nor_a_performer_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, list[str]]],
) -> None:
    config = _config(tmp_path, monkeypatch, {"implementer": {}, "qa": {"provider": "qare"}})

    results = await launch_preflight.check_harnesses(config)

    assert "harness nare" in _failed(results)
    assert ("performer protocol", [""]) in probes


async def test_a_config_with_no_roles_and_no_performer_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, list[str]]],
) -> None:
    config = _config(tmp_path, monkeypatch, {})

    results = await launch_preflight.check_harnesses(config)

    assert "performer transport" in _failed(results)


def test_a_container_endpoint_probes_no_harness_for_a_provider_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, list[str]]],
) -> None:
    monkeypatch.setattr(launch_preflight.shutil, "which", lambda _name: "/usr/bin/docker")
    config = _config(
        tmp_path, monkeypatch,
        {"implementer": {"backend": "codex"}, "qa": {"provider": "qare"}},
        performer_endpoints=[{
            "id": "full", "mode": "ephemeral", "roles": ["implementer", "qa"],
            "image": "coordinare-performer:full",
        }],
    )

    results = launch_preflight._container_harnesses(config)

    assert "container harness codex" in _names(results)
    assert "container harness nare" not in _names(results)


@pytest.mark.parametrize("explicit", [True, False], ids=["doctor", "startup"])
@pytest.mark.parametrize("provider", sorted(_PROVIDERS))
async def test_doctor_and_startup_pass_a_provider_only_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, list[str]]],
    provider: str, explicit: bool,
) -> None:
    """The shared gate: only the provider's own checks speak for a provider-only role."""
    own = CheckResult(f"stage provider '{provider}'", Status.OK, "answered.")

    async def provider_checks(_config: ProjectConfiguration) -> list[CheckResult]:
        return [own]

    async def no_github(*_args: Any, **_kwargs: Any) -> list[CheckResult]:
        return []

    monkeypatch.setattr(launch_preflight, "check_stage_providers", provider_checks)
    monkeypatch.setattr(launch_preflight, "check_github", no_github)
    monkeypatch.setattr(launch_preflight, "check_directories", lambda _config: [])
    monkeypatch.setattr(launch_preflight, "check_port", lambda _config: CheckResult("port", Status.OK, "free."))
    config = _config(tmp_path, monkeypatch, {_ROLE_OF[provider]: {"provider": provider}})

    report = await launch_preflight.run_preflight(config, explicit=explicit)

    assert _failed(report.results) == []
    assert own in report.results
    assert not any(name.startswith("harness ") for name in _names(report.results))
    assert "performer protocol" not in _names(report.results)
