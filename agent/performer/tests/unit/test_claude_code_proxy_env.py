"""073: _load_proxy_env helper and structural FR-005 backend scoping."""
from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from performer.backends.claude_code import ClaudeCodeBackend
from performer.config import Settings, get_settings
from performer.models import Score, Stand


@pytest.fixture(autouse=True)
def _clear_settings_cache(monkeypatch: pytest.MonkeyPatch):
    # Hermetic env: a developer's shell often carries ANTHROPIC_BASE_URL /
    # ANTHROPIC_API_KEY (and possibly LITELLM_PROXY_*). These leak into the
    # subprocess-env-merge assertions (e.g. a real ANTHROPIC_BASE_URL would
    # win over the test's LITELLM_PROXY_BASE_URL), so clear them up front.
    # Individual tests re-set whatever they need via monkeypatch.setenv.
    for _var in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "LITELLM_PROXY_BASE_URL",
        "LITELLM_PROXY_AUTH_TOKEN",
    ):
        monkeypatch.delenv(_var, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _settings(base_url: str = "", token: str = "") -> Settings:
    s = Settings()
    s.LITELLM_PROXY_BASE_URL = base_url
    s.LITELLM_PROXY_AUTH_TOKEN = token
    return s


# ---------------------------------------------------------------------------
# T006 — _load_proxy_env helper
# ---------------------------------------------------------------------------


class TestLoadProxyEnv:
    def test_empty_base_url_returns_empty_dict(self) -> None:
        """FR-003 / SC-002: no env injection when proxy is unconfigured."""
        assert ClaudeCodeBackend._load_proxy_env(_settings()) == {}

    def test_base_url_only_injects_anthropic_base_url(self) -> None:
        env = ClaudeCodeBackend._load_proxy_env(
            _settings(base_url="https://proxy.example.com")
        )
        assert env == {"ANTHROPIC_BASE_URL": "https://proxy.example.com"}
        assert "ANTHROPIC_AUTH_TOKEN" not in env

    def test_base_url_and_token_inject_both(self) -> None:
        env = ClaudeCodeBackend._load_proxy_env(
            _settings(
                base_url="https://proxy.example.com",
                token="sk-litellm-abc123",
            )
        )
        assert env["ANTHROPIC_BASE_URL"] == "https://proxy.example.com"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-litellm-abc123"

    def test_whitespace_base_url_treated_as_empty(self) -> None:
        assert ClaudeCodeBackend._load_proxy_env(_settings(base_url="   ")) == {}


# ---------------------------------------------------------------------------
# T006a — FR-005: other backends MUST NOT reference proxy settings
# ---------------------------------------------------------------------------


class TestBackendScoping:
    @pytest.mark.parametrize(
        "module_name",
        [
            "performer.backends.opencode",
            "performer.backends.junie",
            "performer.backends.codex",
            "performer.backends.hermes",
        ],
    )
    def test_non_claude_backends_do_not_reference_proxy(self, module_name: str) -> None:
        try:
            module = __import__(module_name, fromlist=["*"])
        except ImportError:
            pytest.skip(f"{module_name} not present")
        src = inspect.getsource(module)
        assert "LITELLM_PROXY_BASE_URL" not in src
        assert "LITELLM_PROXY_AUTH_TOKEN" not in src
        assert "ANTHROPIC_BASE_URL" not in src
        assert "ANTHROPIC_AUTH_TOKEN" not in src


# ---------------------------------------------------------------------------
# Subprocess env merge — proxy values win over os.environ
# ---------------------------------------------------------------------------


def _fake_proc() -> MagicMock:
    proc = MagicMock()
    proc.pid = 1
    proc.returncode = None

    async def _empty():
        return
        yield  # pragma: no cover

    proc.stdout = MagicMock()
    proc.stdout.__aiter__ = lambda self: _empty()
    # Reader loop reads stdout.read(n) — return EOF immediately so tests don't
    # spin on an unconfigured MagicMock attribute (which Python 3.14 happens to
    # accept past `await`, producing a non-empty truthy chunk that loops).
    proc.stdout.read = AsyncMock(return_value=b"")
    proc.stderr = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    proc.kill = MagicMock()
    return proc


class TestSubprocessEnvMerge:
    async def test_no_injection_baseline_when_proxy_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SC-002: env dict has no ANTHROPIC_BASE_URL/AUTH_TOKEN keys when unset."""
        monkeypatch.delenv("LITELLM_PROXY_BASE_URL", raising=False)
        monkeypatch.delenv("LITELLM_PROXY_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        get_settings.cache_clear()

        adapter = ClaudeCodeBackend()
        score = Score(
            title="t",
            repo_url="https://github.com/org/repo",
            branch="main",
            github_token="tok",
        )
        stand = Stand(path=tmp_path, branch="main")

        proc = _fake_proc()
        with patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(stand, score)

        env = mock_exec.call_args[1]["env"]
        assert "ANTHROPIC_BASE_URL" not in env
        assert "ANTHROPIC_AUTH_TOKEN" not in env

    async def test_proxy_env_fills_when_os_environ_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Boot-time proxy settings populate ANTHROPIC_* when nothing else has.

        With the shim active (073), the subprocess sees ``ANTHROPIC_BASE_URL``
        pointing at the loopback shim; the upstream URL flows into the shim
        constructor instead.
        """
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("LITELLM_PROXY_BASE_URL", "https://proxy.example.com")
        monkeypatch.setenv("LITELLM_PROXY_AUTH_TOKEN", "sk-litellm-default")
        get_settings.cache_clear()

        adapter = ClaudeCodeBackend()
        score = Score(
            title="t",
            repo_url="https://github.com/org/repo",
            branch="main",
            github_token="tok",
        )
        stand = Stand(path=tmp_path, branch="main")

        shim_instance = MagicMock()
        shim_instance.start = AsyncMock(return_value="http://127.0.0.1:55555")
        shim_instance.stop = AsyncMock()
        shim_cls = MagicMock(return_value=shim_instance)

        proc = _fake_proc()
        with patch(
            "performer.backends.claude_code.ClaudeCodeShim", shim_cls,
        ), patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(stand, score)

        env = mock_exec.call_args[1]["env"]
        # Subprocess sees the loopback shim URL, not the upstream proxy URL.
        assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:55555"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-litellm-default"
        # Shim was constructed with the upstream URL + boot-time token.
        shim_cls.assert_called_once_with(
            "https://proxy.example.com", "sk-litellm-default", capture_dir=""
        )

    async def test_per_job_os_environ_overrides_proxy_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-job ANTHROPIC_* values injected into os.environ by _perform_job
        (from JobInitPayload.secrets) win over boot-time _proxy_env defaults
        AND flow into the shim's operator-bearer header.
        """
        monkeypatch.setenv("LITELLM_PROXY_BASE_URL", "https://proxy.example.com")
        monkeypatch.setenv("LITELLM_PROXY_AUTH_TOKEN", "boot-default-token")
        get_settings.cache_clear()
        adapter = ClaudeCodeBackend()

        # Simulate _perform_job having injected per-job secrets into os.environ
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "per-job-override")

        score = Score(
            title="t",
            repo_url="https://github.com/org/repo",
            branch="main",
            github_token="tok",
        )
        stand = Stand(path=tmp_path, branch="main")

        shim_instance = MagicMock()
        shim_instance.start = AsyncMock(return_value="http://127.0.0.1:55556")
        shim_instance.stop = AsyncMock()
        shim_cls = MagicMock(return_value=shim_instance)

        proc = _fake_proc()
        with patch(
            "performer.backends.claude_code.ClaudeCodeShim", shim_cls,
        ), patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(stand, score)

        env = mock_exec.call_args[1]["env"]
        # Subprocess base URL points at the shim loopback.
        assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:55556"
        # Per-job override propagates to the env the CLI sees.
        assert env["ANTHROPIC_AUTH_TOKEN"] == "per-job-override"
        # Per-job override ALSO flows into the shim constructor — this is the
        # durable coordinare-level override path reaching the upstream proxy.
        shim_cls.assert_called_once_with(
            "https://proxy.example.com", "per-job-override", capture_dir=""
        )

    async def test_shim_start_failure_does_not_fall_back_to_upstream(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T027 / FR-010: if the shim fails to bind, the backend reports an
        error and the subprocess MUST NOT be spawned pointing at the operator's
        upstream URL (no silent direct-routing fallback).
        """
        monkeypatch.setenv("LITELLM_PROXY_BASE_URL", "https://proxy.example.com")
        monkeypatch.setenv("LITELLM_PROXY_AUTH_TOKEN", "sk-litellm")
        get_settings.cache_clear()

        adapter = ClaudeCodeBackend()
        score = Score(
            title="t",
            repo_url="https://github.com/org/repo",
            branch="main",
            github_token="tok",
        )
        stand = Stand(path=tmp_path, branch="main")

        shim_instance = MagicMock()
        shim_instance.start = AsyncMock(side_effect=OSError("port in use"))
        shim_instance.stop = AsyncMock()
        shim_cls = MagicMock(return_value=shim_instance)

        mock_exec = AsyncMock(return_value=_fake_proc())
        with patch(
            "performer.backends.claude_code.ClaudeCodeShim", shim_cls,
        ), patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=mock_exec,
        ):
            await adapter.start(stand, score)

        # Backend reports error state.
        status = adapter.get_status()
        assert status.state == "error"
        # And the subprocess was NEVER spawned with upstream credentials —
        # either it wasn't spawned at all, or if it was, the env did NOT
        # point at the operator's upstream URL.
        if mock_exec.called:
            env = mock_exec.call_args[1].get("env", {})
            assert env.get("ANTHROPIC_BASE_URL") != "https://proxy.example.com"
        else:
            assert not mock_exec.called

    async def test_proxy_failure_surfaces_as_nonzero_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FR-007: proxy auth failure surfaces via existing CLI non-zero-exit path."""
        monkeypatch.setenv("LITELLM_PROXY_BASE_URL", "https://proxy.example.com")
        monkeypatch.setenv("LITELLM_PROXY_AUTH_TOKEN", "sk-litellm-bad")
        get_settings.cache_clear()

        adapter = ClaudeCodeBackend()
        score = Score(
            title="t",
            repo_url="https://github.com/org/repo",
            branch="main",
            github_token="tok",
        )
        stand = Stand(path=tmp_path, branch="main")

        shim_instance = MagicMock()
        shim_instance.start = AsyncMock(return_value="http://127.0.0.1:55557")
        shim_instance.stop = AsyncMock()
        shim_cls = MagicMock(return_value=shim_instance)

        proc = _fake_proc()
        proc.returncode = 1
        proc.wait = AsyncMock(return_value=1)

        with patch(
            "performer.backends.claude_code.ClaudeCodeShim", shim_cls,
        ), patch(
            "performer.backends.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(stand, score)
            # Drive the reader loop to completion to mirror real CLI exit.
            assert adapter._reader_task is not None
            await adapter._reader_task

        status = adapter.get_status()
        assert status.state == "error"
        assert "1" in (status.error_reason or "")
