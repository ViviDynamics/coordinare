"""Exercise deployment preflight verdicts and the smoke-test GitHub boundary."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from coordinare import doctor
from coordinare.bench.coordinare_facing_github_stub import CoordinareFacingGitHubStub
from coordinare.services.kubernetes_egress import EnforcementVerdict


@pytest.mark.asyncio
async def test_smoke_github_serves_startup_queries_and_rejects_unknown_queries() -> None:
    stub = CoordinareFacingGitHubStub(project_title="build-smoke", host="127.0.0.1", port=0)
    await stub.stop()  # teardown is safe even before startup
    await stub.start()
    assert stub._runner is not None
    port = stub._runner.addresses[0][1]
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            project = (await client.post("/graphql", json={"query": "repositoryOwner"})).json()
            assert project["data"]["repositoryOwner"]["projectV2"]["title"] == "build-smoke"
            fields = (await client.post("/graphql", json={"query": "fields (first: 20)"})).json()
            options = fields["data"]["node"]["fields"]["nodes"][0]["options"]
            assert {option["name"] for option in options} == {
                "Backlog", "Ready", "In progress", "In review", "Blocked", "Done",
            }
            assert len({option["id"] for option in options}) == 6
            items = (await client.post("/graphql", json={"query": "items (first: 20)"})).json()
            assert items["data"]["node"]["items"] == {
                "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
            unknown = (await client.post("/graphql", json={"query": "mutation " + "x" * 200})).json()
            assert unknown["errors"][0]["message"] == "stub does not implement: " + ("mutation " + "x" * 200)[:120]
            assert (await client.get("/repos/example/status")).json() == {}
            assert len(stub.queries) == 4
    finally:
        await stub.stop()
    assert stub._runner is None


def test_preflight_probe_reports_connection_timeout_and_http_answers(monkeypatch) -> None:
    get = Mock(side_effect=[httpx.ConnectError("offline"), httpx.ReadTimeout("slow"), httpx.Response(404)])
    monkeypatch.setattr(doctor.httpx, "get", get)
    assert doctor._probe_endpoint("http://model")[0] is False
    assert doctor._probe_endpoint("http://model", timeout=2) == (False, "no response within 2s")
    assert doctor._probe_endpoint("http://model") == (True, "responded with HTTP 404")


@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(200, text="invalid")])
def test_unreadable_model_catalog_is_unknown_not_empty_success(monkeypatch, response) -> None:
    response.request = httpx.Request("GET", "http://model/api/tags")
    monkeypatch.setattr(doctor.httpx, "get", Mock(return_value=response))
    assert doctor._list_ollama_models("http://model/") == []


def test_preflight_reports_model_mismatch_and_native_provider_skip(monkeypatch) -> None:
    config = SimpleNamespace(
        endpoints=[SimpleNamespace(name="local", kind="ollama", base_url="http://model"),
                   SimpleNamespace(name="native", kind="openai", base_url=None)],
        model_endpoints=[SimpleNamespace(name="tool", model="missing", endpoint="local"),
                         SimpleNamespace(name="think", model="native-model", endpoint="native")],
        dashboard_host="127.0.0.1",
    )
    monkeypatch.setattr(doctor, "_probe_endpoint", lambda _: (False, "offline"))
    monkeypatch.setattr(doctor, "_list_ollama_models", lambda _: [f"model-{n}" for n in range(10)])
    report = doctor.run_checks(config)
    assert not report.ok and len(report.failed) == 2
    rendered = report.render()
    assert "ollama pull missing" in rendered and "(+2 more)" in rendered
    assert "[SKIP] endpoint 'native'" in rendered
    assert "2 check(s) failed" in rendered
    monkeypatch.setattr(doctor, "_list_ollama_models", lambda _: [])
    assert doctor.check_models(config)[0].status is doctor.Status.WARN


@pytest.mark.parametrize("enforced,baseline,status,fix_fragment", [
    (False, False, doctor.Status.WARN, "re-run"),
    (False, True, doctor.Status.WARN, "Calico"),
    (True, True, doctor.Status.OK, None),
])
def test_egress_preflight_reports_measured_verdict(monkeypatch, enforced, baseline, status, fix_fragment) -> None:
    from kubernetes import client
    from kubernetes import config as kube_config

    from coordinare.services import kubernetes_egress

    config = SimpleNamespace(agent_transport="kubernetes", kubernetes_namespace="isolated",
                             performer_endpoints=[SimpleNamespace(image="performer:test")])
    monkeypatch.setattr(kube_config, "load_incluster_config", Mock(side_effect=ValueError("outside")))
    load = Mock()
    monkeypatch.setattr(kube_config, "load_kube_config", load)
    core, network = object(), object()
    monkeypatch.setattr(client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(client, "NetworkingV1Api", lambda: network)
    probe = AsyncMock(return_value=EnforcementVerdict(enforced, baseline, "measured"))
    monkeypatch.setattr(kubernetes_egress, "probe_network_policy_enforcement", probe)
    result = doctor.check_kubernetes_egress_enforcement(config)[0]
    assert result.status is status and result.detail == "measured"
    assert result.fix is None if fix_fragment is None else fix_fragment in result.fix
    load.assert_called_once_with()
    probe.assert_awaited_once_with(core_v1=core, networking_v1=network,
                                  namespace="isolated", image="performer:test")


def test_egress_preflight_missing_image_and_credentials_are_actionable(monkeypatch) -> None:
    from kubernetes import config as kube_config

    config = SimpleNamespace(agent_transport="docker", performer_endpoints=[])
    assert doctor.check_kubernetes_egress_enforcement(config)[0].status is doctor.Status.SKIP
    config.agent_transport = "kubernetes"
    assert doctor.check_kubernetes_egress_enforcement(config)[0].status is doctor.Status.SKIP
    config.performer_endpoints = [SimpleNamespace(image="performer:test")]
    monkeypatch.setattr(kube_config, "load_incluster_config", Mock(side_effect=ValueError("outside")))
    monkeypatch.setattr(kube_config, "load_kube_config", Mock(side_effect=ValueError("bad credentials")))
    result = doctor.check_kubernetes_egress_enforcement(config)[0]
    assert result.status is doctor.Status.FAIL and "KUBECONFIG" in result.fix
