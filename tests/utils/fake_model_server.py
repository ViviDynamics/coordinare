from __future__ import annotations

from aiohttp import web


def model_app(token: str, requests: list[str]) -> web.Application:
    """A models API that accepts only the configured stub credential."""
    async def models(request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != "Bearer " + token:
            raise web.HTTPUnauthorized()
        requests.append(request.path)
        return web.json_response({"object": "list", "data": [{"id": "smoke-model", "object": "model"}]})

    app = web.Application()
    app.router.add_get("/v1/models", models)
    return app
