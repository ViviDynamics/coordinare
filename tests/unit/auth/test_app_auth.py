"""Unit tests for AppAuth JWT generation and token caching (015 T031, T032)."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from coordinare.auth.app import AppAuth
from coordinare.services.github import TransientGitHubError

# ---------------------------------------------------------------------------
# RSA key fixture — generated synthetically; no real GitHub credentials
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_key_pair(tmp_path_factory):
    """Generate a synthetic 2048-bit RSA key pair for testing."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    tmp = tmp_path_factory.mktemp("keys")
    key_file = tmp / "test_key.pem"
    key_file.write_bytes(private_pem)
    return key_file, private_pem


@pytest.fixture
def app_auth(rsa_key_pair):
    key_file, _ = rsa_key_pair
    return AppAuth(app_id=12345, private_key_path=key_file, installation_id=67890)


# ---------------------------------------------------------------------------
# T031: JWT generation tests
# ---------------------------------------------------------------------------


class TestJWTGeneration:
    def test_jwt_contains_correct_iss(self, app_auth: AppAuth) -> None:
        import jwt as pyjwt

        token = app_auth._make_jwt()
        # Decode without verification to inspect claims
        claims = pyjwt.decode(token, options={"verify_signature": False})
        assert claims["iss"] == "12345"

    def test_jwt_iat_within_60s_window(self, app_auth: AppAuth) -> None:
        import jwt as pyjwt

        now = int(time.time())
        token = app_auth._make_jwt()
        claims = pyjwt.decode(token, options={"verify_signature": False})
        # iat should be now - 60 (allow 5s variance for test execution)
        assert now - 65 <= claims["iat"] <= now

    def test_jwt_exp_at_most_10_minutes(self, app_auth: AppAuth) -> None:
        import jwt as pyjwt

        now = int(time.time())
        token = app_auth._make_jwt()
        claims = pyjwt.decode(token, options={"verify_signature": False})
        assert claims["exp"] <= now + 600 + 5  # 5s buffer for test execution
        assert claims["exp"] > now  # must be in the future

    def test_jwt_uses_rs256_algorithm(self, app_auth: AppAuth, rsa_key_pair) -> None:
        import jwt as pyjwt
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        _key_file, private_pem = rsa_key_pair
        private_key = load_pem_private_key(private_pem, password=None)
        public_key = private_key.public_key()

        token = app_auth._make_jwt()
        # Verify signature — only works if RS256 was used with the correct key
        claims = pyjwt.decode(token, public_key, algorithms=["RS256"])
        assert claims["iss"] == "12345"

    def test_jwt_is_string(self, app_auth: AppAuth) -> None:
        token = app_auth._make_jwt()
        assert isinstance(token, str)
        assert len(token) > 0


# ---------------------------------------------------------------------------
# T032: Token caching, refresh, concurrency, error handling
# ---------------------------------------------------------------------------


class TestTokenCaching:
    def _make_fetch_response(self, token: str = "inst-tok", seconds_until_expiry: int = 3600):
        return token, time.monotonic() + seconds_until_expiry

    @pytest.mark.asyncio
    async def test_cached_token_returned_when_valid(self, app_auth: AppAuth) -> None:
        # Pre-populate cache with a token that expires far in the future
        app_auth._cached_token = "cached-valid-tok"
        app_auth._token_expires_at = time.monotonic() + 3600  # 1 hour from now

        result = await app_auth.get_token()
        assert result == "cached-valid-tok"

    @pytest.mark.asyncio
    async def test_refresh_triggered_when_less_than_5_min_remaining(self, app_auth: AppAuth) -> None:
        # Token expires in 4 minutes (< REFRESH_BUFFER_SECONDS=300)
        app_auth._cached_token = "nearly-expired-tok"
        app_auth._token_expires_at = time.monotonic() + 240

        new_token = "fresh-tok"

        async def fake_fetch():
            return new_token, time.monotonic() + 3600

        with patch.object(app_auth, "_fetch_installation_token", side_effect=fake_fetch):
            result = await app_auth.get_token()

        assert result == new_token

    @pytest.mark.asyncio
    async def test_fresh_token_fetched_when_cache_empty(self, app_auth: AppAuth) -> None:
        app_auth._cached_token = None
        app_auth._token_expires_at = None

        async def fake_fetch():
            return "brand-new-tok", time.monotonic() + 3600

        with patch.object(app_auth, "_fetch_installation_token", side_effect=fake_fetch):
            result = await app_auth.get_token()

        assert result == "brand-new-tok"

    @pytest.mark.asyncio
    async def test_concurrent_callers_serialised_by_lock(self, app_auth: AppAuth) -> None:
        app_auth._cached_token = None
        app_auth._token_expires_at = None
        fetch_count = 0

        async def fake_fetch():
            nonlocal fetch_count
            fetch_count += 1
            await asyncio.sleep(0)  # yield to let other tasks run
            return "concurrent-tok", time.monotonic() + 3600

        with patch.object(app_auth, "_fetch_installation_token", side_effect=fake_fetch):
            results = await asyncio.gather(*[app_auth.get_token() for _ in range(5)])

        # All callers should get the same token
        assert all(r == "concurrent-tok" for r in results)
        # The fetch should only be called once (lock prevents redundant fetches)
        assert fetch_count == 1

    @pytest.mark.asyncio
    async def test_invalidate_clears_cache_and_forces_remint(self, app_auth: AppAuth) -> None:
        """085 Contract A1: invalidate() clears the cache so the next get_token re-mints."""
        app_auth._cached_token = "stale-tok"
        app_auth._token_expires_at = time.monotonic() + 3600  # otherwise-valid

        result = await app_auth.invalidate()
        assert result is None
        assert app_auth._cached_token is None
        assert app_auth._token_expires_at is None

        async def fake_fetch():
            return "fresh-tok", time.monotonic() + 3600

        with patch.object(app_auth, "_fetch_installation_token", side_effect=fake_fetch):
            assert await app_auth.get_token() == "fresh-tok"

    @pytest.mark.asyncio
    async def test_invalidate_is_safe_under_concurrency(self, app_auth: AppAuth) -> None:
        """085 Contract A3: concurrent invalidate() calls are safe and return None."""
        app_auth._cached_token = "tok"
        app_auth._token_expires_at = time.monotonic() + 3600

        results = await asyncio.gather(*[app_auth.invalidate() for _ in range(5)])
        assert results == [None] * 5
        assert app_auth._cached_token is None
        assert app_auth._token_expires_at is None

    def test_load_key_raises_value_error_on_missing_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.pem"
        with pytest.raises(ValueError, match="does not exist or is not readable"):
            AppAuth._load_key(missing)

    @pytest.mark.asyncio
    async def test_transient_http_error_propagates(self, app_auth: AppAuth) -> None:
        app_auth._cached_token = None
        app_auth._token_expires_at = None

        async def failing_fetch():
            raise TransientGitHubError("HTTP 500")

        with patch.object(app_auth, "_fetch_installation_token", side_effect=failing_fetch), pytest.raises(TransientGitHubError):
            await app_auth.get_token()

    @pytest.mark.asyncio
    async def test_lock_is_not_none_after_construction(self, app_auth: AppAuth) -> None:
        """Lock must be initialised at __init__, not lazily on first get_token()."""
        assert app_auth._lock is not None


# ---------------------------------------------------------------------------
# _fetch_installation_token error classification
# ---------------------------------------------------------------------------


class TestFetchInstallationTokenErrors:
    """Tests for correct error classification in _fetch_installation_token."""

    @pytest.fixture
    def auth(self, rsa_key_pair):
        key_file, _ = rsa_key_pair
        return AppAuth(app_id=1, private_key_path=key_file, installation_id=2)

    @pytest.mark.asyncio
    async def test_401_raises_permanent_error(self, auth: AppAuth) -> None:
        """HTTP 401 (bad credentials) must raise PermanentGitHubError, not transient."""
        from coordinare.services.github import PermanentGitHubError

        mock_response = _make_http_response(401)
        with patch("httpx.AsyncClient.post", return_value=mock_response), pytest.raises(PermanentGitHubError, match="HTTP 401"):
            await auth._fetch_installation_token()

    @pytest.mark.asyncio
    async def test_403_raises_permanent_error(self, auth: AppAuth) -> None:
        """HTTP 403 (insufficient permissions) must raise PermanentGitHubError."""
        from coordinare.services.github import PermanentGitHubError

        mock_response = _make_http_response(403)
        with patch("httpx.AsyncClient.post", return_value=mock_response), pytest.raises(PermanentGitHubError, match="HTTP 403"):
            await auth._fetch_installation_token()

    @pytest.mark.asyncio
    async def test_404_raises_permanent_error(self, auth: AppAuth) -> None:
        """HTTP 404 (installation not found) must raise PermanentGitHubError."""
        from coordinare.services.github import PermanentGitHubError

        mock_response = _make_http_response(404)
        with patch("httpx.AsyncClient.post", return_value=mock_response), pytest.raises(PermanentGitHubError, match="check app_id"):
            await auth._fetch_installation_token()

    @pytest.mark.asyncio
    async def test_500_raises_transient_error(self, auth: AppAuth) -> None:
        """HTTP 500 must raise TransientGitHubError (retryable)."""
        mock_response = _make_http_response(500)
        with patch("httpx.AsyncClient.post", return_value=mock_response), pytest.raises(TransientGitHubError, match="HTTP 500"):
            await auth._fetch_installation_token()

    @pytest.mark.asyncio
    async def test_missing_token_field_raises_transient(self, auth: AppAuth) -> None:
        """Response missing 'token' key raises TransientGitHubError."""
        mock_response = _make_http_response(201, json_body={"expires_at": "2099-01-01T00:00:00Z"})
        with patch("httpx.AsyncClient.post", return_value=mock_response), pytest.raises(TransientGitHubError, match="malformed"):
            await auth._fetch_installation_token()

    @pytest.mark.asyncio
    async def test_missing_expires_at_field_raises_transient(self, auth: AppAuth) -> None:
        """Response missing 'expires_at' key raises TransientGitHubError."""
        mock_response = _make_http_response(201, json_body={"token": "tok"})
        with patch("httpx.AsyncClient.post", return_value=mock_response), pytest.raises(TransientGitHubError, match="malformed"):
            await auth._fetch_installation_token()

    @pytest.mark.asyncio
    async def test_invalid_expires_at_format_raises_transient(self, auth: AppAuth) -> None:
        """Response with unparseable 'expires_at' raises TransientGitHubError."""
        mock_response = _make_http_response(
            201, json_body={"token": "tok", "expires_at": "not-a-date"}
        )
        with patch("httpx.AsyncClient.post", return_value=mock_response), pytest.raises(TransientGitHubError, match="malformed"):
            await auth._fetch_installation_token()

    @pytest.mark.asyncio
    async def test_success_returns_token_and_expiry(self, auth: AppAuth) -> None:
        """HTTP 201 with valid body returns (token, monotonic_deadline)."""
        mock_response = _make_http_response(
            201,
            json_body={"token": "ghs_good", "expires_at": "2099-01-01T00:00:00Z"},
        )
        with patch("httpx.AsyncClient.post", return_value=mock_response):
            token, expires_at = await auth._fetch_installation_token()
        assert token == "ghs_good"
        assert expires_at > time.monotonic()


def _make_http_response(status_code: int, json_body: dict | None = None):
    """Build a minimal httpx.Response stub."""
    import json as json_mod
    body = json_mod.dumps(json_body or {}).encode()
    return httpx.Response(status_code, content=body)


# ---------------------------------------------------------------------------
# 036 — GitHub Enterprise Support: custom API URL for token exchange
# ---------------------------------------------------------------------------


class TestAppAuthCustomAPIURL:
    """Verify AppAuth derives the token exchange URL from api_url."""

    def test_default_api_url(self, rsa_key_pair) -> None:
        key_file, _ = rsa_key_pair
        auth = AppAuth(app_id=1, private_key_path=key_file, installation_id=2)
        assert auth._api_url == "https://api.github.com"

    def test_custom_api_url(self, rsa_key_pair) -> None:
        key_file, _ = rsa_key_pair
        auth = AppAuth(
            app_id=1, private_key_path=key_file, installation_id=2,
            api_url="https://github.acme.corp/api/v3",
        )
        assert auth._api_url == "https://github.acme.corp/api/v3"

    def test_trailing_slash_stripped(self, rsa_key_pair) -> None:
        key_file, _ = rsa_key_pair
        auth = AppAuth(
            app_id=1, private_key_path=key_file, installation_id=2,
            api_url="https://github.acme.corp/api/v3/",
        )
        assert auth._api_url == "https://github.acme.corp/api/v3"

    def test_token_url_uses_custom_api_url(self, rsa_key_pair) -> None:
        key_file, _ = rsa_key_pair
        auth = AppAuth(
            app_id=1, private_key_path=key_file, installation_id=42,
            api_url="https://ghes.internal/api/v3",
        )
        url = auth.TOKEN_URL_TEMPLATE.format(
            api_url=auth._api_url, installation_id=42,
        )
        assert url == "https://ghes.internal/api/v3/app/installations/42/access_tokens"

    @pytest.mark.parametrize(
        ("bad_url", "needle"),
        [
            ("https://github acme.corp", "whitespace"),
            ("ftp://github.acme.corp", "valid http/https"),
            ("https://", "valid http/https"),
            ("https://user:pw@github.acme.corp", "embedded credentials"),
            ("https://github.acme.corp/api?x=1", "query or fragment"),
            ("https://github.acme.corp/api#frag", "query or fragment"),
            ("http://github.acme.corp", "http is only allowed for localhost"),
        ],
    )
    def test_api_url_validation_rejects_bad_inputs(
        self, rsa_key_pair, bad_url: str, needle: str,
    ) -> None:
        key_file, _ = rsa_key_pair
        with pytest.raises(ValueError, match=needle):
            AppAuth(
                app_id=1, private_key_path=key_file, installation_id=2,
                api_url=bad_url,
            )

    @pytest.mark.parametrize(
        "ok_url",
        ["http://localhost:8080", "http://127.0.0.1", "http://[::1]"],
    )
    def test_api_url_allows_http_localhost(self, rsa_key_pair, ok_url: str) -> None:
        key_file, _ = rsa_key_pair
        auth = AppAuth(
            app_id=1, private_key_path=key_file, installation_id=2,
            api_url=ok_url,
        )
        assert auth._api_url == ok_url


# ---------------------------------------------------------------------------
# validate_auth_config tests
# ---------------------------------------------------------------------------


class TestValidateAuthConfig:
    def test_pat_mode_does_not_check_key_file(self, tmp_path: Path) -> None:
        from coordinare.auth import validate_auth_config
        from coordinare.config import ProjectConfiguration

        config = ProjectConfiguration(
            project_name="demo",
            github_org="acme",
            github_project_number=1,
            github_token="tok",
            human_reviewers=["alice"],
        )
        # Should not raise or exit
        validate_auth_config(config)

    def test_app_mode_with_existing_key_passes(self, tmp_path: Path) -> None:
        from coordinare.auth import validate_auth_config
        from coordinare.config import ProjectConfiguration

        key_file = tmp_path / "key.pem"
        key_file.write_text("fake pem")

        config = ProjectConfiguration(
            project_name="demo",
            github_org="acme",
            github_project_number=1,
            human_reviewers=["alice"],
            github_auth="app",
            github_app_id=1,
            github_private_key_path=key_file,
            github_installation_id=2,
        )
        validate_auth_config(config)  # must not raise

    def test_app_mode_missing_key_calls_sys_exit(self, tmp_path: Path) -> None:
        from coordinare.auth import validate_auth_config
        from coordinare.config import ProjectConfiguration

        config = ProjectConfiguration(
            project_name="demo",
            github_org="acme",
            github_project_number=1,
            human_reviewers=["alice"],
            github_auth="app",
            github_app_id=1,
            github_private_key_path=tmp_path / "does_not_exist.pem",
            github_installation_id=2,
        )
        with pytest.raises(SystemExit):
            validate_auth_config(config)
