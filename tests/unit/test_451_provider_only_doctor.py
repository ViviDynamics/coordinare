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
from coordinare.first_launch import running_summary

pytestmark = pytest.mark.asyncio

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
    assert "performer protocol" in [name for name, _command in probes]


async def test_a_provider_role_does_not_hide_a_performer_role_on_another_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, list[str]]],
) -> None:
    """A non-provider role with no local performer and no endpoint still fails."""
    config = _config(
        tmp_path, monkeypatch, {"implementer": {"transport": "ssh"}, "qa": {"provider": "qare"}},
    )

    results = await launch_preflight.check_harnesses(config)

    assert "performer transport" in _failed(results)


async def test_an_endpoint_naming_only_a_provider_role_is_no_http_performer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, list[str]]],
) -> None:
    """An endpoint serves no role a provider fills, so it cannot stand in for a missing performer."""
    config = _config(
        tmp_path, monkeypatch, {"implementer": {"transport": "ssh"}, "qa": {"provider": "qare"}},
        performer_endpoints=[{"id": "q", "mode": "ephemeral", "roles": ["qa"], "image": "img"}],
    )

    results = await launch_preflight.check_harnesses(config, probe_containers=False)

    assert "performer transport" in _failed(results)


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


def test_an_endpoint_serving_only_provider_roles_probes_no_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, list[str]]],
) -> None:
    """The #451 workaround endpoint: its image is checked, but no harness runs in it."""
    monkeypatch.setattr(launch_preflight.shutil, "which", lambda _name: "/usr/bin/docker")
    config = _config(
        tmp_path, monkeypatch, {"qa": {"provider": "qare"}},
        performer_endpoints=[{"id": "qa", "mode": "ephemeral", "roles": ["qa"], "image": "img"}],
    )

    results = launch_preflight._container_harnesses(config)

    assert "performer image qa" in _names(results)
    assert not any(name.startswith("container harness") for name in _names(results))


def test_an_endpoint_with_no_roles_still_probes_its_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, list[str]]],
) -> None:
    monkeypatch.setattr(launch_preflight.shutil, "which", lambda _name: "/usr/bin/docker")
    config = _config(
        tmp_path, monkeypatch, {"qa": {"provider": "qare"}},
        performer_endpoints=[{"id": "spare", "mode": "ephemeral", "roles": [], "image": "img"}],
    )

    results = launch_preflight._container_harnesses(config)

    assert "container harness nare" in _names(results)


def test_startup_checks_no_model_for_a_provider_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider role's mode has no effect, so its models are not probed at startup."""
    catalogs = {
        "endpoints": [{"name": "local", "kind": "vllm", "base_url": "http://localhost:8000"}],
        "model_endpoints": [{"name": "coder", "endpoint": "local", "model": "qwen"}],
        "modes": [{"name": "single", "strategy": "single", "tool": "coder"}],
    }
    provider_only = _config(
        tmp_path, monkeypatch, {"default": {"mode": "single"}, "qa": {"provider": "qare"}}, **catalogs,
    )
    mixed = _config(
        tmp_path, monkeypatch,
        {"default": {"mode": "single"}, "implementer": {}, "qa": {"provider": "qare"}}, **catalogs,
    )

    assert launch_preflight._active_model_config(provider_only).model_endpoints == []
    assert [m.name for m in launch_preflight._active_model_config(mixed).model_endpoints] == ["coder"]


def test_the_running_summary_names_the_provider_of_a_provider_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, monkeypatch, {"implementer": {"backend": "codex"}, "qa": {"provider": "qare"}})

    summary = running_summary(config)

    assert "implementer (codex)" in summary
    assert "qa (provider qare)" in summary


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

    # Startup without a preset downgrades a failure to a warning, so neither may appear.
    assert [r.name for r in report.results if r.status is not Status.OK] == []
    assert own in report.results
    assert not any(name.startswith("harness ") for name in _names(report.results))
    assert "performer protocol" not in _names(report.results)


async def test_a_persistent_endpoint_serving_only_provider_roles_is_not_probed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unused endpoint that is down cannot fail doctor for a provider-only config."""
    asked: list[str] = []

    def unreachable(*_args: Any, **_kwargs: Any) -> Any:
        asked.append("status")
        raise launch_preflight.httpx.ConnectError("down")

    monkeypatch.setattr(launch_preflight, "probe_http", unreachable)
    endpoints = [
        {"id": "qa-only", "mode": "persistent", "roles": ["qa"], "endpoint": "http://qa.test:8080", "image": "img"},
        {"id": "review-stage", "mode": "persistent", "roles": ["reviewing"], "endpoint": "http://r.test:8080", "image": "img"},
        {"id": "mixed", "mode": "persistent", "roles": ["qa", "implementer"], "endpoint": "http://m.test:8080", "image": "img"},
    ]
    config = _config(
        tmp_path, monkeypatch,
        {"implementer": {}, "qa": {"provider": "qare"}, "reviewer": {"provider": "scrutare"}},
        performer_endpoints=endpoints,
    )

    results = await launch_preflight._persistent_harnesses(config)

    assert _names(results) == ["performer mixed"]
    assert asked == ["status"]


async def test_an_endpoint_naming_a_provider_role_by_its_stage_serves_no_performer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, list[str]]],
) -> None:
    """Endpoint roles may name the stage (reviewing) instead of the role (reviewer)."""
    config = _config(
        tmp_path, monkeypatch, {"implementer": {"transport": "ssh"}, "reviewer": {"provider": "scrutare"}},
        performer_endpoints=[{"id": "r", "mode": "ephemeral", "roles": ["reviewing"], "image": "img"}],
    )

    results = await launch_preflight.check_harnesses(config, probe_containers=False)

    assert "performer transport" in _failed(results)


async def test_a_stage_named_endpoint_serves_its_performer_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, list[str]]],
) -> None:
    """An endpoint listing implementing serves implementer, so no local performer is needed."""
    config = _config(
        tmp_path, monkeypatch, {"implementer": {"backend": "codex"}, "qa": {"provider": "qare"}},
        performer_endpoints=[{
            "id": "impl", "mode": "persistent", "roles": ["implementing"],
            "endpoint": "http://impl.test:8080", "image": "img",
        }],
    )

    results = launch_preflight._local_harnesses(config)

    assert _failed(results) == []
    assert probes == []


def test_a_stage_named_container_endpoint_probes_its_roles_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, list[str]]],
) -> None:
    monkeypatch.setattr(launch_preflight.shutil, "which", lambda _name: "/usr/bin/docker")
    config = _config(
        tmp_path, monkeypatch, {"implementer": {"backend": "codex"}, "reviewer": {"provider": "scrutare"}},
        performer_endpoints=[{
            "id": "full", "mode": "ephemeral", "roles": ["implementing", "reviewing"], "image": "img",
        }],
    )

    results = launch_preflight._container_harnesses(config)

    assert [n for n in _names(results) if n.startswith("container harness")] == ["container harness codex"]


async def test_an_endpoint_the_env_bootstrap_uses_is_still_probed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probes: list[tuple[str, list[str]]],
) -> None:
    """A symphony's env_bootstrap_performer_id dispatches to the endpoint by id, whatever its roles."""
    from coordinare.config import CoordinareConfiguration, SymphonyConfig

    def unreachable(*_args: Any, **_kwargs: Any) -> Any:
        raise launch_preflight.httpx.ConnectError("down")

    async def nothing(*_args: Any, **_kwargs: Any) -> list[CheckResult]:
        return []

    monkeypatch.setattr(launch_preflight, "probe_http", unreachable)
    monkeypatch.setattr(launch_preflight, "check_stage_providers", nothing)
    monkeypatch.setattr(launch_preflight, "check_github", nothing)
    monkeypatch.setattr(launch_preflight, "check_directories", lambda _config: [])
    monkeypatch.setattr(launch_preflight, "check_port", lambda _config: CheckResult("port", Status.OK, "free."))
    config = _config(
        tmp_path, monkeypatch, {"qa": {"provider": "qare"}},
        performer_endpoints=[{
            "id": "boot", "mode": "persistent", "roles": ["qa"], "endpoint": "http://boot.test:8080",
            "image": "img",
        }],
    )
    root = CoordinareConfiguration(
        global_config=config,
        symphonies=[SymphonyConfig(name="widgets", github_project_number=1, env_bootstrap_performer_id="boot")],
    )

    report = await launch_preflight.run_preflight(config, root, explicit=True, probe_containers=False)

    assert "performer boot" in _names(report.results)
