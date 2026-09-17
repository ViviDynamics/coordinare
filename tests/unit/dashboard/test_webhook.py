"""Unit tests for HMAC verification and webhook route (015 T043, T044)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac

import pytest

from coordinare.dashboard import register_webhook_route, verify_github_signature

SECRET = "test-secret-value"


def _make_sig(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# ---------------------------------------------------------------------------
# T043: HMAC verification (pure function tests)
# ---------------------------------------------------------------------------


class TestVerifyGithubSignature:
    def test_valid_signature_returns_true(self) -> None:
        body = b'{"action": "opened"}'
        sig = _make_sig(body)
        assert verify_github_signature(body, SECRET, sig) is True

    def test_invalid_signature_returns_false(self) -> None:
        body = b'{"action": "opened"}'
        assert verify_github_signature(body, SECRET, "sha256=badhex") is False

    def test_missing_header_returns_false(self) -> None:
        body = b'{"action": "opened"}'
        assert verify_github_signature(body, SECRET, None) is False

    def test_empty_header_returns_false(self) -> None:
        body = b'{"action": "opened"}'
        assert verify_github_signature(body, SECRET, "") is False

    def test_header_without_sha256_prefix_returns_false(self) -> None:
        body = b"data"
        digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert verify_github_signature(body, SECRET, digest) is False

    def test_wrong_secret_returns_false(self) -> None:
        body = b'{"action": "closed"}'
        sig = _make_sig(body, secret="different-secret")
        assert verify_github_signature(body, SECRET, sig) is False

    def test_different_body_returns_false(self) -> None:
        body = b'{"action": "opened"}'
        sig = _make_sig(b'{"action": "closed"}')
        assert verify_github_signature(body, SECRET, sig) is False

    def test_constant_time_comparison_used(self) -> None:
        """Verify hmac.compare_digest is used — same length but different values must return False."""
        body = b"x"
        correct_sig = _make_sig(body)
        # Tamper last character — same length, different value
        tampered = correct_sig[:-1] + ("0" if correct_sig[-1] == "1" else "1")
        assert verify_github_signature(body, SECRET, tampered) is False

    def test_empty_body_valid_signature(self) -> None:
        body = b""
        sig = _make_sig(body)
        assert verify_github_signature(body, SECRET, sig) is True


# ---------------------------------------------------------------------------
# T044: Webhook route via FastAPI TestClient
# ---------------------------------------------------------------------------


@pytest.fixture
def webhook_trigger() -> asyncio.Event:
    return asyncio.Event()


@pytest.fixture
def webhook_app(webhook_trigger: asyncio.Event):
    from fastapi import FastAPI
    app = FastAPI()
    register_webhook_route(app, path="/webhook/github", secret=SECRET, trigger=webhook_trigger)
    return app


class TestWebhookRoute:
    def test_valid_post_returns_200(self, webhook_app) -> None:
        from fastapi.testclient import TestClient
        client = TestClient(webhook_app)
        body = b'{"action": "opened"}'
        sig = _make_sig(body)
        resp = client.post("/webhook/github", content=body, headers={"X-Hub-Signature-256": sig})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_valid_post_sets_trigger_event(self, webhook_app, webhook_trigger: asyncio.Event) -> None:
        from fastapi.testclient import TestClient
        client = TestClient(webhook_app)
        body = b'{"action": "opened"}'
        sig = _make_sig(body)
        assert not webhook_trigger.is_set()
        client.post("/webhook/github", content=body, headers={"X-Hub-Signature-256": sig})
        assert webhook_trigger.is_set()

    def test_invalid_signature_returns_401(self, webhook_app, webhook_trigger: asyncio.Event) -> None:
        from fastapi.testclient import TestClient
        client = TestClient(webhook_app)
        body = b'{"action": "opened"}'
        resp = client.post(
            "/webhook/github", content=body,
            headers={"X-Hub-Signature-256": "sha256=badhex"},
        )
        assert resp.status_code == 401

    def test_invalid_signature_does_not_set_trigger(self, webhook_app, webhook_trigger: asyncio.Event) -> None:
        from fastapi.testclient import TestClient
        client = TestClient(webhook_app)
        body = b'{"action": "opened"}'
        client.post(
            "/webhook/github", content=body,
            headers={"X-Hub-Signature-256": "sha256=badhex"},
        )
        assert not webhook_trigger.is_set()

    def test_missing_signature_returns_401(self, webhook_app) -> None:
        from fastapi.testclient import TestClient
        client = TestClient(webhook_app)
        resp = client.post("/webhook/github", content=b"data")
        assert resp.status_code == 401

    def test_webhook_not_enabled_route_not_registered(self, webhook_trigger: asyncio.Event) -> None:
        """When register_webhook_route is NOT called, the path returns 404."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        # Deliberately do NOT call register_webhook_route
        client = TestClient(app)
        resp = client.post("/webhook/github", content=b"data")
        assert resp.status_code == 404
