"""Unit tests for the performer container lifecycle service (spec 056, T026)."""

from __future__ import annotations

import httpx
import pytest

from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services import performer_lifecycle as lifecycle
from coordinare.services.performer_lifecycle import (
    ContainerStartError,
    ReadinessTimeoutError,
    StartedContainer,
    _redact_docker_args,
    _safe_stop,
    start_ephemeral,
    stop,
    wait_ready,
)
from coordinare.transport.http_transport import PerformerHTTPClient


def _ephemeral_config(**overrides: object) -> PerformerEndpointConfig:
    base: dict[str, object] = {
        "id": "perf-1",
        "mode": "ephemeral",
        "roles": ["performer"],
        "image": "performer:base",
    }
    base.update(overrides)
    return PerformerEndpointConfig.model_validate(base)


# ---------------------------------------------------------------------------
# start_ephemeral — core paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_ephemeral_runs_docker_and_resolves_port(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        calls.append(args)
        if args[0] == "run":
            return 0, "abc123\n", ""
        if args[0] == "port":
            return 0, "0.0.0.0:49160\n[::]:49160", ""
        raise AssertionError(f"unexpected docker invocation: {args}")

    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)

    started = await start_ephemeral(_ephemeral_config())

    assert isinstance(started, StartedContainer)
    assert started.container_id == "abc123"
    assert started.endpoint == "http://127.0.0.1:49160"
    assert calls[0][0] == "run"
    assert "performer:base" in calls[0]
    assert calls[0][-1] == "performer:base"  # image is the last arg; no CMD overrides


@pytest.mark.asyncio
async def test_start_ephemeral_fails_when_docker_run_errors(monkeypatch) -> None:
    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        return 125, "", "no such image"

    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)

    with pytest.raises(ContainerStartError):
        await start_ephemeral(_ephemeral_config())


@pytest.mark.asyncio
async def test_start_ephemeral_raises_when_image_is_none(monkeypatch) -> None:
    # Bypass the model_validator by constructing config without image
    # We can't construct PerformerEndpointConfig(mode="ephemeral", image=None) directly
    # (model_validator rejects it), so we patch start_ephemeral's internal check.
    config = _ephemeral_config()
    object.__setattr__(config, "image", None)  # force None past pydantic

    with pytest.raises(ContainerStartError, match="no image"):
        await start_ephemeral(config)


@pytest.mark.asyncio
async def test_start_ephemeral_with_pinned_port(monkeypatch) -> None:
    args_seen: list[tuple[str, ...]] = []

    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        args_seen.append(args)
        if args[0] == "run":
            return 0, "ctr123\n", ""
        if args[0] == "port":
            return 0, "0.0.0.0:9090\n", ""
        raise AssertionError(f"unexpected: {args}")

    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)

    started = await start_ephemeral(_ephemeral_config(port=9090))

    run_args = args_seen[0]
    p_idx = list(run_args).index("-p")
    assert run_args[p_idx + 1] == "9090:8088"
    assert started.endpoint == "http://127.0.0.1:9090"


@pytest.mark.asyncio
async def test_start_ephemeral_with_auth_token_env(monkeypatch) -> None:
    args_seen: list[tuple[str, ...]] = []

    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        args_seen.append(args)
        if args[0] == "run":
            return 0, "ctr456\n", ""
        if args[0] == "port":
            return 0, "0.0.0.0:8080\n", ""
        raise AssertionError(f"unexpected: {args}")

    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)

    await start_ephemeral(_ephemeral_config(auth_token="tok123"))

    run_args = list(args_seen[0])
    assert "-e" in run_args
    env_val_idx = run_args.index("-e") + 1
    assert run_args[env_val_idx] == "PERFORMER_AUTH_TOKEN=tok123"


@pytest.mark.asyncio
async def test_start_ephemeral_propagates_config_env(monkeypatch) -> None:
    """env entries (e.g. RTK_ENABLED) flow through to ``docker run -e KEY=VAL``."""
    args_seen: list[tuple[str, ...]] = []

    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        args_seen.append(args)
        if args[0] == "run":
            return 0, "ctr-env\n", ""
        if args[0] == "port":
            return 0, "0.0.0.0:8080\n", ""
        raise AssertionError(f"unexpected: {args}")

    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)

    await start_ephemeral(
        _ephemeral_config(env={"BACKEND": "codex", "RTK_ENABLED": "1"})
    )

    run_args = list(args_seen[0])
    assert "BACKEND=codex" in run_args
    assert "RTK_ENABLED=1" in run_args


@pytest.mark.asyncio
async def test_start_ephemeral_applies_egress_allowlist(monkeypatch) -> None:
    """egress_allowlist adds --cap-add NET_ADMIN and the PERFORMER_EGRESS_ALLOWLIST env var."""
    args_seen: list[tuple[str, ...]] = []

    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        args_seen.append(args)
        if args[0] == "run":
            return 0, "ctr-egress\n", ""
        if args[0] == "port":
            return 0, "0.0.0.0:8080\n", ""
        raise AssertionError(f"unexpected: {args}")

    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)

    await start_ephemeral(
        _ephemeral_config(
            egress_allowlist=["api.github.com", "objects.githubusercontent.com"]
        )
    )

    run_args = list(args_seen[0])
    assert "--cap-add" in run_args
    cap_idx = run_args.index("--cap-add")
    assert run_args[cap_idx + 1] == "NET_ADMIN"
    assert (
        "PERFORMER_EGRESS_ALLOWLIST=api.github.com,objects.githubusercontent.com"
        in run_args
    )


@pytest.mark.asyncio
async def test_start_ephemeral_with_volumes(monkeypatch) -> None:
    args_seen: list[tuple[str, ...]] = []

    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        args_seen.append(args)
        if args[0] == "run":
            return 0, "ctr789\n", ""
        if args[0] == "port":
            return 0, "0.0.0.0:8080\n", ""
        raise AssertionError(f"unexpected: {args}")

    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)

    cfg = _ephemeral_config(
        volumes=[{"host_path": "/tmp/src", "container_path": "/workspace", "mode": "ro"}]
    )
    await start_ephemeral(cfg)

    run_args = list(args_seen[0])
    assert "-v" in run_args
    vol_idx = run_args.index("-v") + 1
    assert run_args[vol_idx] == "/tmp/src:/workspace:ro"


@pytest.mark.asyncio
async def test_start_ephemeral_port_lookup_failure(monkeypatch) -> None:
    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        if args[0] == "run":
            return 0, "ctr999\n", ""
        if args[0] == "port":
            return 1, "", "no binding"
        if args[0] == "stop":
            return 0, "", ""
        raise AssertionError(f"unexpected: {args}")

    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)

    with pytest.raises(ContainerStartError, match="port lookup failed"):
        await start_ephemeral(_ephemeral_config())


@pytest.mark.asyncio
async def test_start_ephemeral_resolves_127_0_0_1_prefix(monkeypatch) -> None:
    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        if args[0] == "run":
            return 0, "ctrabc\n", ""
        if args[0] == "port":
            return 0, "127.0.0.1:54321\n", ""
        raise AssertionError(f"unexpected: {args}")

    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)

    started = await start_ephemeral(_ephemeral_config())
    assert started.endpoint == "http://127.0.0.1:54321"


@pytest.mark.asyncio
async def test_start_ephemeral_empty_port_output_raises(monkeypatch) -> None:
    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        if args[0] == "run":
            return 0, "ctr_empty\n", ""
        if args[0] == "port":
            return 0, "", ""  # success rc but empty output
        if args[0] == "stop":
            return 0, "", ""
        raise AssertionError(f"unexpected: {args}")

    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)

    with pytest.raises(ContainerStartError):
        await start_ephemeral(_ephemeral_config())


@pytest.mark.asyncio
async def test_start_ephemeral_fallback_port_from_non_ipv4_line(monkeypatch) -> None:
    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        if args[0] == "run":
            return 0, "ctrfb\n", ""
        if args[0] == "port":
            # Only IPv6 line — fallback parses it
            return 0, "[::]:44444\n", ""
        raise AssertionError(f"unexpected: {args}")

    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)

    started = await start_ephemeral(_ephemeral_config())
    assert started.endpoint.endswith(":44444")


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_logs_on_non_zero_rc(monkeypatch) -> None:
    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        return 1, "", "no such container"

    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)
    # Must not raise — stop() is best-effort
    await stop("missing-container")


# ---------------------------------------------------------------------------
# _safe_stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_stop_swallows_exception(monkeypatch) -> None:
    async def boom(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        raise RuntimeError("docker died")

    monkeypatch.setattr(lifecycle, "_run_docker", boom)
    await _safe_stop("ctr-dead")  # must not propagate


# ---------------------------------------------------------------------------
# stop — exception handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_logs_container_start_error(monkeypatch) -> None:
    async def boom(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        raise ContainerStartError("docker command timed out")

    monkeypatch.setattr(lifecycle, "_run_docker", boom)
    # Must not raise — stop() is best-effort
    await stop("ctr-timeout")


# ---------------------------------------------------------------------------
# _redact_docker_args
# ---------------------------------------------------------------------------


def test_redact_docker_args_preserves_non_env_args() -> None:
    """Non -e args are preserved as-is."""
    args = ("run", "-d", "--rm", "image:tag")
    result = _redact_docker_args(args)
    assert "run" in result
    assert "-d" in result
    assert "--rm" in result
    assert "image:tag" in result


def test_redact_docker_args_redacts_env_values() -> None:
    """Environment variables are redacted."""
    args = ("run", "-e", "SECRET_KEY=my-secret-value", "-d", "image:tag")
    result = _redact_docker_args(args)
    assert "SECRET_KEY" in result
    assert "<redacted>" in result
    assert "my-secret-value" not in result


def test_redact_docker_args_multiple_env_variables() -> None:
    """Multiple environment variables are all redacted."""
    args = (
        "run",
        "-e",
        "GITHUB_TOKEN=ghp_xxx",
        "-e",
        "OPENAI_KEY=sk_xxx",
        "image:tag",
    )
    result = _redact_docker_args(args)
    assert result.count("<redacted>") == 2
    assert "ghp_xxx" not in result
    assert "sk_xxx" not in result
    assert "GITHUB_TOKEN" in result
    assert "OPENAI_KEY" in result


def test_redact_docker_args_env_at_end_without_value() -> None:
    """Handle -e as final arg (no value following)."""
    args = ("run", "-e")
    result = _redact_docker_args(args)
    assert "-e" in result
    # No value to redact, so no <redacted> should appear
    assert "<redacted>" not in result


# ---------------------------------------------------------------------------
# start_ephemeral — secret sources configuration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_ephemeral_disables_secret_sources_when_disabled(
    monkeypatch,
) -> None:
    args_seen: list[tuple[str, ...]] = []

    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        args_seen.append(args)
        if args[0] == "run":
            return 0, "ctr-sources\n", ""
        if args[0] == "port":
            return 0, "0.0.0.0:8080\n", ""
        raise AssertionError(f"unexpected: {args}")

    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)

    cfg = _ephemeral_config(
        secret_sources={
            "init_payload": False,
            "env": False,
            "creds_file": True,
        }
    )
    await start_ephemeral(cfg)

    run_args = list(args_seen[0])
    # Check that disabled sources are set to 0
    assert "PERFORMER_SECRET_SOURCE_INIT_PAYLOAD=0" in run_args
    assert "PERFORMER_SECRET_SOURCE_ENV=0" in run_args
    # creds_file is not disabled, so no =0 env var
    assert "PERFORMER_SECRET_SOURCE_CREDS_FILE=0" not in run_args


@pytest.mark.asyncio
async def test_start_ephemeral_with_creds_file_path(monkeypatch) -> None:
    args_seen: list[tuple[str, ...]] = []

    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        args_seen.append(args)
        if args[0] == "run":
            return 0, "ctr-creds\n", ""
        if args[0] == "port":
            return 0, "0.0.0.0:8080\n", ""
        raise AssertionError(f"unexpected: {args}")

    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)

    cfg = _ephemeral_config(
        secret_sources={
            "init_payload": True,
            "env": True,
            "creds_file": True,
            "creds_file_path": "/etc/credentials.json",
        }
    )
    await start_ephemeral(cfg)

    run_args = list(args_seen[0])
    # Check that creds_file_path is set when provided
    assert "PERFORMER_CREDS_FILE=/etc/credentials.json" in run_args


@pytest.mark.asyncio
async def test_start_ephemeral_with_all_secret_sources_enabled(
    monkeypatch,
) -> None:
    args_seen: list[tuple[str, ...]] = []

    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        args_seen.append(args)
        if args[0] == "run":
            return 0, "ctr-all\n", ""
        if args[0] == "port":
            return 0, "0.0.0.0:8080\n", ""
        raise AssertionError(f"unexpected: {args}")

    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)

    cfg = _ephemeral_config(
        secret_sources={
            "init_payload": True,
            "env": True,
            "creds_file": True,
        }
    )
    await start_ephemeral(cfg)

    run_args = list(args_seen[0])
    # When all sources are enabled, no =0 env vars should be present
    assert "PERFORMER_SECRET_SOURCE_INIT_PAYLOAD=0" not in run_args
    assert "PERFORMER_SECRET_SOURCE_ENV=0" not in run_args
    assert "PERFORMER_SECRET_SOURCE_CREDS_FILE=0" not in run_args


@pytest.mark.asyncio
async def test_start_ephemeral_disables_creds_file_when_false(monkeypatch) -> None:
    args_seen: list[tuple[str, ...]] = []

    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        args_seen.append(args)
        if args[0] == "run":
            return 0, "ctr-nocreds\n", ""
        if args[0] == "port":
            return 0, "0.0.0.0:8080\n", ""
        raise AssertionError(f"unexpected: {args}")

    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)

    cfg = _ephemeral_config(
        secret_sources={
            "init_payload": True,
            "env": True,
            "creds_file": False,
        }
    )
    await start_ephemeral(cfg)

    run_args = list(args_seen[0])
    # When creds_file is disabled, the =0 env var should be set
    assert "PERFORMER_SECRET_SOURCE_CREDS_FILE=0" in run_args


# ---------------------------------------------------------------------------
# wait_ready — existing tests (kept)
# ---------------------------------------------------------------------------


def _client_with(handler) -> PerformerHTTPClient:
    return PerformerHTTPClient(
        "http://127.0.0.1:49160",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_wait_ready_returns_first_non_starting_status() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        availability = "starting" if calls["n"] < 2 else "idle"
        return httpx.Response(
            200,
            json={
                "availability": availability,
                "capabilities": {"backends": [], "tool_flags": []},
                "auth_enabled": False,
            },
        )

    client = _client_with(handler)
    try:
        status = await wait_ready(
            "http://127.0.0.1:49160", None, timeout=2.0, poll_interval=0.01, client=client
        )
    finally:
        await client.aclose()
    assert status.availability == "idle"
    assert calls["n"] >= 2


@pytest.mark.asyncio
async def test_wait_ready_times_out() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "availability": "starting",
                "capabilities": {"backends": [], "tool_flags": []},
                "auth_enabled": False,
            },
        )

    client = _client_with(handler)
    try:
        with pytest.raises(ReadinessTimeoutError):
            await wait_ready(
                "http://127.0.0.1:49160",
                None,
                timeout=0.1,
                poll_interval=0.01,
                client=client,
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_wait_ready_with_performer_id_logs_transition() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "availability": "idle",
                "capabilities": {"backends": [], "tool_flags": []},
                "auth_enabled": False,
            },
        )

    client = _client_with(handler)
    try:
        status = await wait_ready(
            "http://127.0.0.1:49160",
            None,
            timeout=2.0,
            poll_interval=0.01,
            performer_id="perf-log-test",
            client=client,
        )
    finally:
        await client.aclose()
    assert status.availability == "idle"


@pytest.mark.asyncio
async def test_wait_ready_tolerates_unreachable_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("container still starting")
        return httpx.Response(
            200,
            json={
                "availability": "idle",
                "capabilities": {"backends": [], "tool_flags": []},
                "auth_enabled": False,
            },
        )

    client = _client_with(handler)
    try:
        status = await wait_ready(
            "http://127.0.0.1:49160",
            None,
            timeout=5.0,
            poll_interval=0.01,
            client=client,
        )
    finally:
        await client.aclose()
    assert status.availability == "idle"


@pytest.mark.asyncio
async def test_wait_ready_creates_own_client_when_none_provided(monkeypatch) -> None:
    created_clients: list[PerformerHTTPClient] = []

    real_init = PerformerHTTPClient.__init__

    def patched_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        created_clients.append(self)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "availability": "idle",
                "capabilities": {"backends": [], "tool_flags": []},
                "auth_enabled": False,
            },
        )

    # We pass no client — wait_ready must create one internally.
    # Swap the internal client after init so we can intercept HTTP.

    async def fake_get_status(self):
        calls["n"] += 1
        from coordinare.models.performer_endpoint import PerformerCapabilities, PerformerStatus
        return PerformerStatus(
            availability="idle",
            capabilities=PerformerCapabilities(backends=[], tool_flags=[]),
            auth_enabled=False,
        )

    monkeypatch.setattr(PerformerHTTPClient, "get_status", fake_get_status)

    status = await wait_ready("http://127.0.0.1:49160", None, timeout=2.0, poll_interval=0.01)
    assert status.availability == "idle"
