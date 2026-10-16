"""Exercise chart adoption through the browser and real ASGI routes without sockets."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from playwright.sync_api import Page, Route, expect
from pydantic import SecretStr

from coordinare.dashboard_auth import DashboardAuthentication
from coordinare.services.config_seed import seed_config
from tests.unit.test_284_persistent_config import _dashboard

TOKEN = "chart-adoption-dashboard-token-with-sufficient-length"
NEW_INTERVAL = 77


@pytest.mark.e2e
def test_dashboard_selects_reviews_and_confirms_chart_adoption(
    page: Page,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, live, client, _ = _dashboard(tmp_path, monkeypatch)
    client.app.add_middleware(DashboardAuthentication, token=SecretStr(TOKEN))
    assert client.get("/api/config/chart").status_code == 401
    updated = yaml.safe_load(seed.read_text())
    updated.update(
        github_org="adopted-org",
        poll_interval_seconds=NEW_INTERVAL,
        github_token="${NEW_GITHUB_TOKEN}",
    )
    monkeypatch.setenv("NEW_GITHUB_TOKEN", "ghp_browser_secret")
    seed.write_text(yaml.safe_dump(updated))
    before = live.read_bytes()

    def serve_asgi(route: Route) -> None:
        request = route.request
        if urlsplit(request.url).hostname != "127.0.0.1" or urlsplit(request.url).path == "/events":
            route.fulfill(status=200, content_type="text/plain", body="")
            return
        response = client.request(
            request.method, request.url, content=request.post_data, headers=request.headers,
        )
        headers = {
            key: value
            for key, value in response.headers.items()
            if key not in {"content-length", "content-encoding"}
        }
        route.fulfill(status=response.status_code, headers=headers, body=response.content)

    page.set_extra_http_headers({"Authorization": f"Bearer {TOKEN}"})
    page.route("**/*", serve_asgi)
    page.goto("http://127.0.0.1:8090/config")
    page.get_by_role("button", name="Adopt chart values", exact=True).click()
    review = page.locator("#cfg-chart-review")
    expect(review).to_contain_text("adopted-org")
    expect(review).not_to_contain_text("${NEW_GITHUB_TOKEN}")
    expect(review).not_to_contain_text("ghp_browser_secret")
    assert live.read_bytes() == before
    review.locator('[data-chart-section="poll_interval_seconds"]').uncheck()
    review.locator('[data-chart-section="github_token"]').uncheck()
    review.get_by_role("button", name="Confirm adoption", exact=True).click()
    expect(review.locator("[data-chart-status]")).to_contain_text("Chart sections saved.")
    assert yaml.safe_load(live.read_text())["github_org"] == "adopted-org"
    assert yaml.safe_load(live.read_text())["github_token"] == "${GITHUB_TOKEN}"
    seed_config(seed, live)
    assert yaml.safe_load(live.read_text())["github_org"] == "adopted-org"


@pytest.mark.e2e
def test_chart_adoption_lifecycle_through_authenticated_asgi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, live, client, daemon = _dashboard(tmp_path, monkeypatch)
    client.app.add_middleware(DashboardAuthentication, token=SecretStr(TOKEN))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    original = yaml.safe_load(live.read_text())
    original["poll_interval_seconds"] = NEW_INTERVAL
    live.write_text(yaml.safe_dump(original))
    chart = yaml.safe_load(seed.read_text())
    chart["github_org"] = "adopted-org"
    seed.write_text(yaml.safe_dump(chart))
    assert client.get("/api/config/chart").status_code == 401
    preview = client.get("/api/config/chart", headers=headers)
    assert preview.status_code == 200
    request = {
        "sections": ["github_org"],
        "seed_hash": preview.json()["seed_hash"],
        "confirmed": True,
    }
    guarded = {**headers, "If-Match": preview.headers["ETag"]}
    assert (
        client.post(
            "/api/config/chart/adopt",
            json=request,
            headers={**guarded, "Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    response = client.post("/api/config/chart/adopt", json=request, headers=guarded)
    assert response.status_code == 200, response.text
    assert daemon._config_reload_trigger.is_set()
    seed_config(seed, live)
    assert yaml.safe_load(live.read_text())["github_org"] == "adopted-org"
    assert yaml.safe_load(live.read_text())["poll_interval_seconds"] == NEW_INTERVAL
    assert client.post("/api/config/chart/adopt", json=request, headers=guarded).status_code == 409
