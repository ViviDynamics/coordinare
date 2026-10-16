"""Regression coverage for lifecycle-critical closer transport startup failures."""
from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from coordinare.__main__ import _build_circuit_breakers, _build_performer_services
from coordinare.config import PerformerRoleConfig, PerformersConfig, ProjectConfiguration


def _config(**roles: PerformerRoleConfig) -> ProjectConfiguration:
    return ProjectConfiguration(
        project_name="Test", github_org="acme", github_project_number=1,
        github_token="tok", human_reviewers=["alice"],
        performers=PerformersConfig(**roles),
    )


@pytest.mark.parametrize(
    ("transport", "cause_type"),
    [("ssh", NotImplementedError), ("kubernetes", ValueError)],
)
def test_enabled_closer_transport_failure_aborts_startup(
    transport: str, cause_type: type[Exception],
) -> None:
    config = _config(closer=PerformerRoleConfig(transport=transport))

    with capture_logs() as logs, pytest.raises(ValueError, match="closer") as raised:
        _build_performer_services(config, _build_circuit_breakers(config))

    assert isinstance(raised.value.__cause__, cause_type)
    diagnostic = next(
        entry for entry in logs
        if entry["event"] == "performer_transport_build_failed.required_role"
    )
    assert diagnostic["log_level"] == "error"
    assert diagnostic["role"] == "closer"
    assert diagnostic["stage"] == "closing_review"
    assert diagnostic["transport"] == transport
    assert diagnostic["error"] == str(raised.value.__cause__)


def test_closer_failure_from_global_transport_aborts_startup() -> None:
    config = _config(closer=PerformerRoleConfig())
    config.agent_transport = "kubernetes"
    with capture_logs(), pytest.raises(ValueError, match="closer"):
        _build_performer_services(config, _build_circuit_breakers(config))


@pytest.mark.parametrize("role", ["implementer", "reviewer", "security", "qa"])
def test_other_roles_retain_warning_and_skip_behavior(role: str) -> None:
    config = _config(**{role: PerformerRoleConfig(transport="ssh")})
    with capture_logs() as logs:
        services = _build_performer_services(config, _build_circuit_breakers(config))
    assert services == {}
    assert any(
        entry["event"] == "performer_transport_build_failed.role_skipped"
        and entry["log_level"] == "warning" and entry["role"] == role
        for entry in logs
    )


@pytest.mark.parametrize(
    "closer", [None, PerformerRoleConfig(), PerformerRoleConfig(transport="ssh", max_concurrency=0)],
    ids=["absent", "http-only", "disabled"],
)
def test_intentionally_unbuilt_closer_does_not_abort(closer: PerformerRoleConfig | None) -> None:
    config = _config(**({"closer": closer} if closer is not None else {}))
    assert _build_performer_services(config, _build_circuit_breakers(config)) == {}


def test_valid_closer_still_builds_one_service() -> None:
    config = _config(closer=PerformerRoleConfig(executable="fake-binary", max_concurrency=3))
    services = _build_performer_services(config, _build_circuit_breakers(config))
    assert set(services) == {"closing_review"}
    assert len(_build_performer_services._service_lists["closing_review"]) == 1


def test_noncritical_role_retains_partial_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinare.__main__ import _build_transport_for_role

    config = _config(implementer=PerformerRoleConfig(executable="fake-binary", max_concurrency=2))
    transport = _build_transport_for_role(config.performers.implementer, config)
    attempts = iter([transport, ValueError("second transport unavailable")])

    def build_transport(*args: object) -> object:
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("coordinare.__main__._build_transport_for_role", build_transport)
    with capture_logs() as logs:
        services = _build_performer_services(config, _build_circuit_breakers(config))
    assert set(services) == {"implementing"}
    assert len(_build_performer_services._service_lists["implementing"]) == 1
    assert any(
        entry["event"] == "performer_transport_build_failed.partial_concurrency"
        and entry["built"] == 1 for entry in logs
    )


@pytest.mark.parametrize("mode", ["ephemeral", "persistent"])
@pytest.mark.parametrize("endpoint_role", ["closer", "closing_review"])
def test_kubernetes_http_only_closer_reaches_composed_service(
    mode: str, endpoint_role: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordinare.__main__ import _build_http_performer_services, _compose_performer_pools
    from coordinare.models.performer_endpoint import PerformerEndpointConfig
    from coordinare.services.http_performer_service import HTTPPerformerService

    # Credential loading is an external boundary; keep the real runtime and
    # HTTP composition without depending on the host's Kubernetes credentials.
    monkeypatch.setattr("coordinare.services.kubernetes_runtime._load_kube_config", lambda: None)
    config = _config(closer=PerformerRoleConfig())
    config.agent_transport = "kubernetes"
    config.performer_endpoints = [PerformerEndpointConfig(
        id="closer-http", mode=mode, roles=[endpoint_role],
        image="test-performer:latest", endpoint="http://127.0.0.1:9001",
    )]
    services = _build_performer_services(config, _build_circuit_breakers(config))
    http_services, bootstrap = _build_http_performer_services(config)
    pools, caps, _ = _compose_performer_pools(
        config=config, service_lists={}, http_services_by_stage=http_services,
        performer_services=services, bootstrap_services_by_id=bootstrap,
    )
    assert isinstance(services["closing_review"], HTTPPerformerService)
    assert pools["closing_review"] == [services["closing_review"]]
    assert caps["closing_review"] == 1


def test_explicit_broken_closer_transport_is_fatal_even_with_http_coverage() -> None:
    from coordinare.models.performer_endpoint import PerformerEndpointConfig

    config = _config(closer=PerformerRoleConfig(transport="kubernetes"))
    config.agent_transport = "kubernetes"
    config.performer_endpoints = [PerformerEndpointConfig(
        id="closer-http", mode="ephemeral", roles=["closer"], image="test-performer:latest",
    )]
    with capture_logs(), pytest.raises(ValueError, match="closer"):
        _build_performer_services(config, _build_circuit_breakers(config))
