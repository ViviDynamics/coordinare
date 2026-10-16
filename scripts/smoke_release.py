from __future__ import annotations

import asyncio
import re
import sys
from importlib import metadata, resources
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import yaml
from coordinare_service_inference.schema import ServicesManifest
from coordinare_service_inference.templater import render

import coordinare
from coordinare.config import PersonasConfig
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.dashboard.assets import _page_html, _sign_in_html, _signed_out_html
from coordinare.doctor import DoctorReport
from coordinare.metrics import CoordinareMetrics
from coordinare.observability import HealthRegistry
from coordinare.services.persona_service import VALID_ROLES, get_effective_instructions

HTTP_OK = 200


async def smoke(version: str) -> None:
    """Run from outside the checkout with only the release tool environment."""
    assert metadata.version("coordinare") == version
    for package in ("coordinare-ci-detection", "coordinare-routing-models", "coordinare-service-inference"):
        assert metadata.version(package) == version
    assert coordinare.__file__ is not None
    assert Path(coordinare.__file__).is_relative_to(Path(sys.prefix))
    assert version in DoctorReport().render()
    assert version in _page_html(True)
    assert "Sign in" in _sign_in_html()
    assert "signed out" in _signed_out_html().lower()
    for role in VALID_ROLES:
        assert get_effective_instructions(role, PersonasConfig()).strip()
    examples = resources.files("coordinare").joinpath("data")
    assert list(examples.iterdir())
    for example in examples.iterdir():
        assert isinstance(yaml.safe_load(example.read_text()), dict)
    manifest = ServicesManifest(agent_version=version, services=[])
    scripts = render(manifest)
    assert scripts.start and scripts.stop and scripts.health

    daemon = MagicMock()
    daemon.state = {"phase": "idle"}
    app = create_dashboard_app(DashboardStore(), daemon, CoordinareMetrics(), HealthRegistry())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8090") as client,
    ):
        page = await client.get("/")
        assert page.status_code == HTTP_OK
        assert f"coordinare {version}" in page.text
        paths = set(re.findall(r'(?:src|href)="(/static/[^\"]+)"', page.text))
        assert paths
        paths.add("/static/activity-streams.js")
        for path in paths:
            response = await client.get(path)
            assert response.status_code == HTTP_OK, path
            assert response.content, path


if __name__ == "__main__":
    asyncio.run(smoke(sys.argv[1]))
