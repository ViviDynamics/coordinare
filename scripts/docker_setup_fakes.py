from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiohttp import web
from tests.utils.fake_model_server import model_app

from coordinare.bench.fake_github_server import FakeGitHubServer
from coordinare.services.fake_github import FakeGitHubService

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class ConnectivityFakes:
    github_url: str = ""
    model_url: str = ""
    github_requests: list[str] = field(default_factory=list)
    model_requests: list[str] = field(default_factory=list)

    @property
    def probe_environment(self) -> dict[str, str]:
        return {
            "COORDINARE_SMOKE_GITHUB_URL": self.github_url + "/user",
            "COORDINARE_SMOKE_GITHUB_TOKEN": "smoke-token",
            "COORDINARE_SMOKE_MODEL_URL": self.model_url + "/models",
            "COORDINARE_SMOKE_MODEL_TOKEN": "smoke-key",
            "COORDINARE_SMOKE_MODEL_KIND": "litellm",
        }

    def assert_probed(self) -> None:
        if not self.github_requests or not self.model_requests:
            raise RuntimeError("Container did not reach both authenticated GitHub and model fakes")


@asynccontextmanager
async def connectivity_fakes(
    directory: Path, *, bind_host: str = "0.0.0.0", gateway_host: str = "host.docker.internal",
):
    """Serve the bench GitHub app and a model fake across the Docker bridge."""
    fakes = ConnectivityFakes()
    bare = directory / "unused.git"
    fake = FakeGitHubService(bare_repo_path=bare, work_dir=directory / "fake-ci")
    github = FakeGitHubServer(fake, bare_repo=bare, head_ref_index={}, scratch=directory)._build_app()

    @web.middleware
    async def authenticate(request, handler):
        if request.headers.get("Authorization") != "Bearer smoke-token":
            raise web.HTTPUnauthorized()
        return await handler(request)

    async def user(request):
        fakes.github_requests.append(request.path)
        return web.json_response({"login": "smoke"})

    github.middlewares.append(authenticate)
    github.router.add_get("/user", user)
    model = model_app("smoke-key", fakes.model_requests)
    async with AsyncExitStack() as stack:
        urls = []
        for app in (github, model):
            runner = web.AppRunner(app)
            await runner.setup()
            stack.push_async_callback(runner.cleanup)
            await web.TCPSite(runner, bind_host, 0).start()
            urls.append(f"http://{gateway_host}:{runner.addresses[0][1]}")
        fakes.github_url, fakes.model_url = urls[0], urls[1] + "/v1"
        yield fakes
