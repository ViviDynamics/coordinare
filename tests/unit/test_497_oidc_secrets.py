"""User Story 5 (497): the client secret never leaks through config or errors."""
from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from coordinare.config import DashboardOidcConfig
from tests.unit.dashboard.oidc_double import CLIENT_ID, ISSUER

SECRET = "live-oidc-secret-do-not-echo-0123456789"


def _config(**overrides) -> DashboardOidcConfig:
    fields: dict[str, object] = {
        "discovery_url": f"{ISSUER}/.well-known/openid-configuration",
        "client_id": CLIENT_ID,
        "client_secret": SecretStr(SECRET),
        "redirect_url": "https://coordinare.example/oidc/callback",
    }
    fields.update(overrides)
    return DashboardOidcConfig.model_validate(fields)


def test_validation_error_does_not_echo_client_secret():
    with pytest.raises(ValidationError) as exc_info:
        _config(discovery_url="http://insecure.test/.well-known/openid-configuration")
    assert SECRET not in str(exc_info.value)


def test_discovery_url_must_be_https():
    with pytest.raises(ValidationError, match="https"):
        _config(discovery_url="http://insecure.test/.well-known/openid-configuration")


def test_session_hours_are_bounded():
    with pytest.raises(ValidationError, match="session_hours"):
        _config(session_hours=0)
    with pytest.raises(ValidationError, match="session_hours"):
        _config(session_hours=169)
    assert _config(session_hours=1).session_hours == 1
    assert _config(session_hours=168).session_hours == 168


def test_redirect_host_must_be_dashboard_or_trusted():
    from coordinare.dashboard_oidc import validate_dashboard_oidc

    validate_dashboard_oidc(_config(), dashboard_host="127.0.0.1", trusted_hosts=["coordinare.example"])
    validate_dashboard_oidc(_config(), dashboard_host="coordinare.example", trusted_hosts=[])
    with pytest.raises(ValueError, match="redirect_url"):
        validate_dashboard_oidc(_config(), dashboard_host="127.0.0.1", trusted_hosts=[])


def test_redirect_host_validation_never_echoes_the_secret():
    from coordinare.dashboard_oidc import validate_dashboard_oidc

    with pytest.raises(ValueError) as exc_info:
        validate_dashboard_oidc(_config(), dashboard_host="127.0.0.1", trusted_hosts=[])
    assert SECRET not in str(exc_info.value)


def test_redirect_host_comparison_is_case_insensitive():
    from coordinare.dashboard_oidc import validate_dashboard_oidc

    validate_dashboard_oidc(
        _config(redirect_url="https://Coordinare.Example/oidc/callback"),
        dashboard_host="127.0.0.1",
        trusted_hosts=["Coordinare.Example"],
    )


def test_wildcard_bind_host_is_never_a_trusted_redirect_target():
    from coordinare.dashboard_oidc import validate_dashboard_oidc

    with pytest.raises(ValueError, match="redirect_url"):
        validate_dashboard_oidc(
            _config(redirect_url="https://0.0.0.0/oidc/callback"),
            dashboard_host="0.0.0.0",
            trusted_hosts=[],
        )


def test_config_api_never_serializes_dashboard_oidc(temp_config_path):
    import yaml

    from tests.unit.test_dashboard_config_api import _make_client

    raw = yaml.safe_load(temp_config_path.read_text())
    raw["dashboard_oidc"] = {
        "discovery_url": f"{ISSUER}/.well-known/openid-configuration",
        "client_id": CLIENT_ID,
        "client_secret": SECRET,
        "redirect_url": "https://coordinare.example/oidc/callback",
    }
    temp_config_path.write_text(yaml.safe_dump(raw))
    client = _make_client(temp_config_path, raw)
    for path in ("/api/config/all", "/api/config/section/global", "/api/config/global"):
        response = client.get(path)
        assert response.status_code == 200
        assert SECRET not in response.text
        assert "dashboard_oidc" not in response.text


def test_secret_str_repr_never_reveals_value():
    config = _config()
    assert SECRET not in repr(config.client_secret)
    assert SECRET not in str(config.client_secret)
    assert SECRET not in repr(config)
