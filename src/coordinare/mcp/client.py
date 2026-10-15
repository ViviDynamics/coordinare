"""Thin async REST client of the dashboard API plus the uniform ToolResult mapping."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import httpx
import structlog
from pydantic import BaseModel

if TYPE_CHECKING:
    from coordinare.mcp.config import McpServerConfig

log = structlog.get_logger(__name__)

ERROR_KIND = Literal[
    "auth_failed", "conflict", "validation", "daemon_unreachable", "daemon_error",
]

#: FR5/R4: daemon 5xx bodies are truncated before surfacing to the agent.
_MAX_DETAIL_CHARS = 500

#: get_stream_head never buffers more than this much stream output (FR4).
_MAX_STREAM_HEAD_BYTES = 64 * 1024

_HTTP_OK = 200
_HTTP_REDIRECT_MAX = 300
_HTTP_CONFLICT = 409
_HTTP_SERVER_ERROR = 500

_CONFLICT_RECOVERY_HINT = (
    "re-read via get_config and retry with the fresh base_hash"
)

_UNREACHABLE_RECOVERY_HINT = (
    "the daemon may be mid-cycle; probe a read-only tool first before retrying"
)


class ToolError(BaseModel):
    """Structured failure for agents: stable kind, HTTP status, detail, hint."""

    kind: ERROR_KIND
    status: int
    detail: str
    recovery_hint: str


class ToolResult(BaseModel):
    """Uniform return type for every MCP tool handler (data-model.md)."""

    ok: bool
    data: dict[str, Any] | None = None
    error: ToolError | None = None

    @classmethod
    def failure(cls, kind: ERROR_KIND, status: int, detail: str, recovery_hint: str) -> ToolResult:
        return cls(ok=False, error=ToolError(
            kind=kind, status=status, detail=detail, recovery_hint=recovery_hint,
        ))


def _truncate(text: str) -> str:
    return text[:_MAX_DETAIL_CHARS]


class DashboardClient:
    """Per-call httpx wrapper of the dashboard REST API (no pooling, no retries).

    Every response maps through the FR4 error table into a :class:`ToolResult`.
    The dashboard token rides the internal hop only and is never logged.
    """

    def __init__(
        self,
        config: McpServerConfig,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._base_url = config.dashboard_url
        self._health_url = config.health_url
        self._dashboard_token = config.dashboard_token
        self._transport = transport

    @property
    def read_budget(self) -> float:
        """Read-timeout budget in seconds (FR5)."""
        return self._config.read_timeout_seconds

    @property
    def lifecycle_budget(self) -> float:
        """Lifecycle-timeout budget in seconds (FR5)."""
        return self._config.lifecycle_timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._dashboard_token.get_secret_value()}",
        }

    async def get_json(self, path: str, *, timeout_seconds: float) -> ToolResult:
        return await self._send(
            self._build("GET", path, base_url=self._base_url), timeout_seconds=timeout_seconds,
        )

    async def get_text(self, path: str, *, timeout_seconds: float) -> ToolResult:
        return await self._send(
            self._build("GET", path, base_url=self._health_url), timeout_seconds=timeout_seconds,
        )

    async def get_stream_head(
        self, path: str, *, timeout_seconds: float, silence_seconds: float = 0.5,
    ) -> ToolResult:
        """Read the buffered head of an SSE-ish stream endpoint without proxying it.

        The dashboard's stream endpoints send their buffered lines immediately
        and then never end, so the tool reads until the socket goes quiet for
        ``silence_seconds`` (httpx read timeout per socket read) or
        ``_MAX_STREAM_HEAD_BYTES`` is reached — never holds an agent on an open
        stream (FR4). Read-timeout during iteration means silence, not failure.
        """
        timeout = httpx.Timeout(timeout_seconds, read=silence_seconds)
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=timeout, transport=self._transport,
            ) as http:
                request = http.build_request("GET", path, headers=self._headers())
                response = await http.send(request, stream=True)
                try:
                    if response.status_code != _HTTP_OK:
                        await response.aread()
                        return self._map_response(response)
                    chunks: list[bytes] = []
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            chunks.append(chunk)
                        if sum(len(c) for c in chunks) >= _MAX_STREAM_HEAD_BYTES:
                            break
                except (httpx.TimeoutException, httpx.ReadError):
                    pass  # silence or the disconnect that follows it — head is all we get
                finally:
                    await response.aclose()
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            return self._unreachable(path, exc)
        return ToolResult(ok=True, data={"text": b"".join(chunks).decode(errors="replace")})

    def _unreachable(self, path: str, exc: Exception) -> ToolResult:
        log.info("mcp_client.daemon_unreachable", path=path, error_type=type(exc).__name__)
        return ToolResult.failure(
            "daemon_unreachable",
            0,
            f"daemon unreachable ({type(exc).__name__})",
            _UNREACHABLE_RECOVERY_HINT,
        )

    async def send_json(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout_seconds: float,
        if_match: str | None = None,
    ) -> ToolResult:
        request = self._build(method, path, base_url=self._base_url, json=json, if_match=if_match)
        return await self._send(request, timeout_seconds=timeout_seconds)

    async def get_with_version(
        self, path: str, *, timeout_seconds: float,
    ) -> tuple[ToolResult, str | None]:
        """A GET and the version it carries in its ``ETag`` header (257), or ``None``.

        The version is the value a write sends back as ``If-Match``. It is returned as the
        daemon sent it, quotes and all, because that is the form ``If-Match`` takes.
        """
        request = self._build("GET", path, base_url=self._base_url)
        outcome = await self._exchange(request, timeout_seconds=timeout_seconds)
        if isinstance(outcome, ToolResult):
            return outcome, None
        return self._map_response(outcome), outcome.headers.get("ETag")

    def _build(
        self,
        method: str,
        path: str,
        *,
        base_url: str,
        json: dict[str, Any] | None = None,
        if_match: str | None = None,
    ) -> httpx.Request:
        headers = self._headers()
        if if_match is not None:
            headers["If-Match"] = if_match
        return httpx.Request(method, f"{base_url.rstrip('/')}{path}", json=json, headers=headers)

    async def _send(self, request: httpx.Request, *, timeout_seconds: float) -> ToolResult:
        outcome = await self._exchange(request, timeout_seconds=timeout_seconds)
        if isinstance(outcome, ToolResult):
            return outcome
        return self._map_response(outcome)

    async def _exchange(
        self, request: httpx.Request, *, timeout_seconds: float,
    ) -> httpx.Response | ToolResult:
        """One request; the response, or the ToolResult for a daemon that could not be reached."""
        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds, transport=self._transport,
            ) as client:
                return await client.send(request)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            log.info(
                "mcp_client.daemon_unreachable", path=request.url.path,
                error_type=type(exc).__name__,
            )
            return ToolResult.failure(
                "daemon_unreachable",
                0,
                f"daemon unreachable ({type(exc).__name__})",
                _UNREACHABLE_RECOVERY_HINT,
            )

    def _map_response(self, response: httpx.Response) -> ToolResult:
        status = response.status_code
        if _HTTP_OK <= status < _HTTP_REDIRECT_MAX:
            if response.headers.get("content-type", "").startswith("application/json"):
                return ToolResult(ok=True, data=response.json())
            return ToolResult(ok=True, data={"text": response.text})
        body = response.text
        if status in (401, 403):
            return ToolResult.failure("auth_failed", status, _truncate(body), "")
        if status == _HTTP_CONFLICT:
            return ToolResult.failure("conflict", status, body, _CONFLICT_RECOVERY_HINT)
        if status in (400, 422):
            return ToolResult.failure("validation", status, body, "")
        kind: ERROR_KIND = "daemon_error" if status >= _HTTP_SERVER_ERROR else "validation"
        # 404/405/anything else: the agent asked for a missing resource; surface it.
        return ToolResult.failure(kind, status, _truncate(body), "")


class ClientNotConfiguredError(RuntimeError):
    """get_client() before set_client(); boot via coordinare.mcp.server."""

    def __init__(self) -> None:
        super().__init__("DashboardClient not configured; boot via coordinare.mcp.server")


_client: DashboardClient | None = None


def set_client(client: DashboardClient) -> None:
    """Install the per-process client (called once by server.build_asgi_app)."""
    global _client
    _client = client


def get_client() -> DashboardClient:
    """Return the installed client; raises when the server has not booted."""
    if _client is None:
        raise ClientNotConfiguredError()
    return _client


__all__ = [
    "ClientNotConfiguredError",
    "DashboardClient",
    "ToolError",
    "ToolResult",
    "get_client",
    "set_client",
]
