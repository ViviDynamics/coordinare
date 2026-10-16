"""Endpoint API verification and model enumeration without inference (#324)."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from coordinare import doctor
from coordinare.config import Endpoint, ModelEndpoint, ProjectConfiguration
from coordinare.setup_reports import doctor_checks

BASE_URL = "http://models.example/v1/"
API_KEY = "private-provider-credential"


@pytest.fixture
def config() -> ProjectConfiguration:
    return ProjectConfiguration(
        github_token="test-token", github_org="example", human_reviewers=["operator"],
        endpoints=[Endpoint(name="local", kind="litellm", base_url=BASE_URL)],
        model_endpoints=[ModelEndpoint(name="tool", model="glm-5.3-flash", endpoint="local")],
    )


def mock_http(monkeypatch, handler, *, follow_redirects=False) -> None:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=follow_redirects)
    monkeypatch.setattr(doctor.httpx, "get", client.get)
    monkeypatch.setattr("coordinare.network_probe.time.sleep", lambda _: None)


@pytest.mark.parametrize("code,status", [(200, "ok"), (204, "ok"), (299, "ok"), (301, "warn"), (401, "warn"), (403, "warn"), (404, "warn"), (429, "warn"), (500, "warn")])
def test_endpoint_verifies_models_route(monkeypatch, config, code, status) -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(code)

    mock_http(monkeypatch, handler)
    result = doctor.check_endpoints(config)[0]
    assert result.status.value == status
    assert all(str(request.url) == "http://models.example/v1/models" for request in requests)
    assert all(request.method == "GET" for request in requests)
    if status == "warn":
        assert "http://models.example/v1/models" in result.fix
        assert "base_url" in result.fix and "/v1" in result.fix
    report = doctor_checks(doctor.DoctorReport([result]))[0]
    assert report["id"] == "doctor.endpoint"
    assert report["status"] == status
    if status == "warn":
        assert "/models" in report["fix"] and "/v1" in report["fix"]


def test_endpoint_connection_refused_remains_warning(monkeypatch, config) -> None:
    def handler(request):
        raise httpx.ConnectError(API_KEY, request=request)

    mock_http(monkeypatch, handler)
    result = doctor.check_endpoints(config)[0]
    assert result.status is doctor.Status.WARN
    assert result.fix and "start the service" in result.fix
    assert API_KEY not in doctor.DoctorReport([result]).render()


@pytest.mark.parametrize("kind", ["litellm", "vllm", "ollama", "openai-compatible"])
@pytest.mark.parametrize("models,status", [(["glm-5.3-flash"], "ok"), (["other-model"], "warn"), ([], "warn")])
def test_models_enumerated_for_all_compatible_kinds(monkeypatch, config, kind, models, status) -> None:
    # Unknown future compatible kinds must not be excluded by a provider allowlist.
    config.endpoints = [SimpleNamespace(name="local", kind=kind, base_url=BASE_URL, auth_env=None)]
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"object": "list", "data": [{"id": model} for model in models]})

    mock_http(monkeypatch, handler)
    result = doctor.check_models(config)[0]
    assert result.status.value == status
    assert requests and all(str(request.url) == "http://models.example/v1/models" for request in requests)
    assert all(request.method == "GET" for request in requests)
    if models and status == "warn":
        assert "other-model" in result.fix
    assert doctor_checks(doctor.DoctorReport([result]))[0]["id"] == "doctor.model"


@pytest.mark.parametrize("response", [
    httpx.Response(404), httpx.Response(401), httpx.Response(500), httpx.Response(204),
    httpx.Response(200, text="invalid JSON"), httpx.Response(200, json={}),
    httpx.Response(200, json=[]), httpx.Response(200, json={"data": None}),
    httpx.Response(200, json={"data": ["invalid"]}),
    httpx.Response(200, json={"data": [{"id": 123}]}),
])
def test_enumeration_unavailable_is_skip(monkeypatch, config, response) -> None:
    mock_http(monkeypatch, lambda request: response)
    assert doctor.check_models(config)[0].status is doctor.Status.SKIP


def test_model_listing_connection_error_is_skip(monkeypatch, config) -> None:
    def handler(request):
        raise httpx.ConnectError(API_KEY, request=request)

    mock_http(monkeypatch, handler)
    result = doctor.check_models(config)[0]
    assert result.status is doctor.Status.SKIP
    assert API_KEY not in doctor.DoctorReport([result]).render()


@pytest.mark.parametrize("kind,endpoint_url", [("litellm", BASE_URL), ("openai", None)])
def test_model_listing_uses_configured_auth(monkeypatch, config, kind, endpoint_url) -> None:
    config.endpoints = [Endpoint(name="local", kind=kind, base_url=endpoint_url, auth_env="TEST_MODEL_KEY")]
    monkeypatch.setenv("TEST_MODEL_KEY", API_KEY)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "glm-5.3-flash"}]})

    mock_http(monkeypatch, handler)
    result = doctor.check_models(config)[0]
    assert result.status is doctor.Status.OK
    assert requests[0].headers["Authorization"] == "Bearer " + API_KEY
    assert str(requests[0].url).endswith("/v1/models")
    assert API_KEY not in doctor.DoctorReport([result]).render()


def test_native_harness_login_without_api_key_skips_enumeration(monkeypatch, config) -> None:
    config.endpoints = [Endpoint(name="local", kind="openai")]
    mock_http(monkeypatch, lambda request: pytest.fail("Native enumeration requires an API key"))
    assert doctor.check_models(config)[0].status is doctor.Status.SKIP


@pytest.mark.parametrize("code", [200, 404])
def test_diagnostics_redact_credentials_and_bound_served_ids(monkeypatch, config, code) -> None:
    config.endpoints[0].base_url = "http://private-user:private-password@models.example/v1?api_key=query-secret"
    config.endpoints[0].auth_env = "TEST_MODEL_KEY"
    monkeypatch.setenv("TEST_MODEL_KEY", API_KEY)
    served = ["model-a", "model-b", API_KEY, "http://private-user:private-password@models.example/model"]
    served += ["z" * 1000 + str(index) for index in range(20)]
    mock_http(monkeypatch, lambda request: httpx.Response(code, json={"data": [{"id": model} for model in served]}))
    results = doctor.check_endpoints(config) + doctor.check_models(config)
    rendered = doctor.DoctorReport(results).render()
    for secret in (API_KEY, "private-user", "private-password", "query-secret"):
        assert secret not in rendered
    if code == 200:
        assert "model-a" in results[1].fix and "model-b" in results[1].fix
        assert len(results[1].fix) < 1200
        assert "more)" in results[1].fix


def test_endpoint_probe_uses_configured_auth(monkeypatch, config) -> None:
    config.endpoints[0].auth_env = "TEST_MODEL_KEY"
    monkeypatch.setenv("TEST_MODEL_KEY", API_KEY)

    def handler(request):
        return httpx.Response(200 if request.headers.get("Authorization") == "Bearer " + API_KEY else 401)

    mock_http(monkeypatch, handler)
    assert doctor.check_endpoints(config)[0].status is doctor.Status.OK


@pytest.mark.parametrize("credential_source", ["env", "userinfo"])
def test_custom_openai_url_keeps_credentials_on_configured_host(monkeypatch, config, credential_source) -> None:
    # Exercise defensive handling even though current config validation rejects
    # native kinds with a base_url; doctor must never reroute their credentials.
    config.endpoints[0].kind = "openai"
    if credential_source == "env":
        config.endpoints[0].auth_env = "TEST_MODEL_KEY"
        monkeypatch.setenv("TEST_MODEL_KEY", API_KEY)
        authorization = "Bearer " + API_KEY
    else:
        config.endpoints[0].base_url = "http://private-user:private-password@models.example/v1"
        authorization = "Basic cHJpdmF0ZS11c2VyOnByaXZhdGUtcGFzc3dvcmQ="
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "glm-5.3-flash"}]})

    mock_http(monkeypatch, handler)
    results = doctor.check_endpoints(config) + doctor.check_models(config)
    assert all(result.status is doctor.Status.OK for result in results)
    assert len(requests) == 2
    assert all(str(request.url) == "http://models.example/v1/models" for request in requests)
    assert all(request.headers["Authorization"] == authorization for request in requests)


@pytest.mark.parametrize("credential_source", ["env", "userinfo"])
def test_endpoint_and_model_checks_do_not_follow_cross_host_redirects(monkeypatch, config, credential_source) -> None:
    if credential_source == "env":
        config.endpoints[0].auth_env = "TEST_MODEL_KEY"
        monkeypatch.setenv("TEST_MODEL_KEY", API_KEY)
    else:
        config.endpoints[0].base_url = "http://private-user:private-password@models.example/v1"
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "models.example":
            return httpx.Response(307, headers={"Location": "https://other.example/v1/models"})
        return httpx.Response(200, json={"data": [{"id": "glm-5.3-flash"}]})

    # Explicitly disabling redirects must also override a client's default.
    mock_http(monkeypatch, handler, follow_redirects=True)
    endpoint_result = doctor.check_endpoints(config)[0]
    model_result = doctor.check_models(config)[0]
    assert all(request.url.host == "models.example" for request in requests)
    assert not any(request.url.host == "other.example" and "Authorization" in request.headers for request in requests)
    assert endpoint_result.status is doctor.Status.WARN
    assert model_result.status is doctor.Status.SKIP


@pytest.mark.parametrize("endpoint_url,route", [
    ("http://localhost:11434", "/api/tags"),
    ("http://localhost:11434/", "/api/tags"),
    ("http://localhost:11434/v1", "/v1/models"),
    ("http://localhost:11434/v1/", "/v1/models"),
])
@pytest.mark.parametrize("model_ids,model_status", [
    (["glm-5.3-flash"], doctor.Status.OK),
    (["other-model"], doctor.Status.WARN),
    ([], doctor.Status.WARN),
])
def test_ollama_root_and_v1_catalogs(monkeypatch, config, endpoint_url, route, model_ids, model_status) -> None:
    config.endpoints = [Endpoint(name="local", kind="ollama", base_url=endpoint_url)]
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": model} for model in model_ids]})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": model} for model in model_ids]})
        return httpx.Response(404)

    mock_http(monkeypatch, handler)
    endpoint_result = doctor.check_endpoints(config)[0]
    model_result = doctor.check_models(config)[0]
    assert endpoint_result.status is doctor.Status.OK
    assert model_result.status is model_status
    assert route in endpoint_result.detail
    assert len(requests) == 2 and all(request.url.path == route for request in requests)
    if model_status is doctor.Status.WARN:
        assert "ollama pull glm-5.3-flash" in model_result.fix
        if model_ids:
            assert "other-model" in model_result.fix


@pytest.mark.asyncio
@pytest.mark.parametrize("code,model_ids,endpoint_status,model_status", [
    (200, ["glm-5.3-flash"], "ok", "ok"),
    (200, ["other-model"], "ok", "warn"),
    (404, [], "warn", "skip"),
])
async def test_init_shared_preflight_only_lists_models(
    monkeypatch, config, code, model_ids, endpoint_status, model_status,
) -> None:
    from unittest.mock import AsyncMock

    from coordinare import launch_preflight

    monkeypatch.setattr(launch_preflight, "validate_start_time", lambda *args: None)
    monkeypatch.setattr(launch_preflight, "check_directories", lambda _: [])
    monkeypatch.setattr(launch_preflight, "check_port", lambda _: doctor.CheckResult("port", doctor.Status.OK, "free"))
    monkeypatch.setattr(launch_preflight, "check_github", AsyncMock(return_value=[]))
    monkeypatch.setattr(launch_preflight, "check_harnesses", AsyncMock(return_value=[]))
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(code, json={"data": [{"id": model} for model in model_ids]})

    mock_http(monkeypatch, handler)
    # init calls this same explicit preflight before offering to start the daemon.
    report = await launch_preflight.run_preflight(config, explicit=True, probe_containers=False)
    checks = {check["id"]: check for check in doctor_checks(report)}
    assert report.ok
    assert checks["doctor.endpoint"]["status"] == endpoint_status
    assert checks["doctor.model"]["status"] == model_status
    assert len(requests) == 2
    assert all(request.method == "GET" and request.url.path == "/v1/models" for request in requests)


@pytest.mark.parametrize("credential", ["tiny", API_KEY])
def test_served_ids_redact_even_short_keys_and_unrelated_url_userinfo(monkeypatch, config, credential) -> None:
    config.endpoints[0].auth_env = "TEST_MODEL_KEY"
    monkeypatch.setenv("TEST_MODEL_KEY", credential)
    mock_http(monkeypatch, lambda request: httpx.Response(200, json={"data": [
        {"id": credential}, {"id": "http://catalog-user:catalog-pass@models.example/served"},
    ]}))
    rendered = doctor.DoctorReport(doctor.check_models(config)).render()
    for secret in (credential, "catalog-user", "catalog-pass"):
        assert secret not in rendered


def test_url_userinfo_is_sent_as_auth_without_logging_it(monkeypatch, config, caplog) -> None:
    config.endpoints[0].base_url = "http://private-user:private-password@models.example/v1"
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "glm-5.3-flash"}]})

    mock_http(monkeypatch, handler)
    results = doctor.check_endpoints(config) + doctor.check_models(config)
    assert all(result.status is doctor.Status.OK for result in results)
    assert all(str(request.url) == "http://models.example/v1/models" for request in requests)
    assert all(request.headers["Authorization"] == "Basic cHJpdmF0ZS11c2VyOnByaXZhdGUtcGFzc3dvcmQ=" for request in requests)
    assert "private-user" not in caplog.text and "private-password" not in caplog.text
