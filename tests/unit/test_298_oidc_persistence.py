from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from coordinare.__main__ import _build_dashboard_oidc_flow
from coordinare.dashboard_oidc import SESSION_COOKIE, OidcFlow, OidcIdentity, OidcSessionStore
from tests.unit.dashboard.oidc_double import ProviderDouble
from tests.unit.test_497_oidc_session import BASE, _app, _log_in, _oidc_config

LIFETIME_SECONDS = 60
START_TIME = 1000.0
SMALL_CAPACITY = 2
PRIVATE_MODE = stat.S_IRUSR | stat.S_IWUSR
PUBLIC_MODE = PRIVATE_MODE | stat.S_IRGRP | stat.S_IROTH
IDENTITY = OidcIdentity(
    subject="operator", name="Operator", email="operator@example.test",
    username="operator", provider="issuer.example", id_token="provider-logout-hint",
)


@pytest.fixture
def session_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "dashboard_oidc_sessions.json"


def test_restart_round_trip_preserves_identity_and_original_expiry(session_path: Path) -> None:
    now = [START_TIME]
    store = OidcSessionStore(path=session_path, clock=lambda: now[0])
    cookie = store.create(IDENTITY, lifetime_seconds=LIFETIME_SECONDS)
    now[0] += LIFETIME_SECONDS / SMALL_CAPACITY
    restarted = OidcSessionStore(path=session_path, clock=lambda: now[0])
    assert restarted.identity(cookie) == IDENTITY
    assert restarted.lookup(cookie) == IDENTITY.subject
    now[0] = START_TIME + LIFETIME_SECONDS
    assert restarted.lookup(cookie) is None


def test_logout_persists_without_removing_other_sessions(session_path: Path) -> None:
    store = OidcSessionStore(path=session_path)
    revoked = store.create("revoked", lifetime_seconds=LIFETIME_SECONDS)
    retained = store.create(IDENTITY, lifetime_seconds=LIFETIME_SECONDS)
    OidcSessionStore(path=session_path).revoke(revoked)
    restarted = OidcSessionStore(path=session_path)
    assert restarted.lookup(revoked) is None
    assert restarted.identity(retained) == IDENTITY
    assert hashlib.sha256(revoked.encode()).hexdigest() not in session_path.read_text()


@pytest.mark.parametrize("on_startup", [False, True])
def test_expiry_removes_sessions_from_disk(session_path: Path, on_startup: bool) -> None:
    now = [START_TIME]
    store = OidcSessionStore(path=session_path, clock=lambda: now[0])
    cookie = store.create(IDENTITY, lifetime_seconds=LIFETIME_SECONDS)
    now[0] += LIFETIME_SECONDS
    if on_startup:
        store = OidcSessionStore(path=session_path, clock=lambda: now[0])
    assert store.lookup(cookie) is None
    assert json.loads(session_path.read_text())["sessions"] == {}
    assert OidcSessionStore(path=session_path, clock=lambda: START_TIME).lookup(cookie) is None


def test_capacity_evicts_oldest_and_persists_eviction(session_path: Path) -> None:
    store = OidcSessionStore(path=session_path, capacity=SMALL_CAPACITY)
    oldest = store.create("oldest", lifetime_seconds=LIFETIME_SECONDS)
    retained = store.create("retained", lifetime_seconds=LIFETIME_SECONDS)
    store = OidcSessionStore(path=session_path, capacity=SMALL_CAPACITY)
    newest = store.create("newest", lifetime_seconds=LIFETIME_SECONDS)
    restarted = OidcSessionStore(path=session_path, capacity=SMALL_CAPACITY)
    assert restarted.lookup(oldest) is None
    assert restarted.lookup(retained) == "retained"
    assert restarted.lookup(newest) == "newest"
    assert len(json.loads(session_path.read_text())["sessions"]) == SMALL_CAPACITY


def test_loading_smaller_capacity_trims_disk(session_path: Path) -> None:
    store = OidcSessionStore(path=session_path)
    oldest = store.create("oldest", lifetime_seconds=LIFETIME_SECONDS)
    retained = store.create("retained", lifetime_seconds=LIFETIME_SECONDS)
    restarted = OidcSessionStore(path=session_path, capacity=1)
    assert restarted.lookup(oldest) is None
    assert restarted.lookup(retained) == "retained"
    assert len(json.loads(session_path.read_text())["sessions"]) == 1


def test_create_prunes_expired_before_evicting_live_session(session_path: Path) -> None:
    now = [START_TIME]
    store = OidcSessionStore(path=session_path, clock=lambda: now[0], capacity=SMALL_CAPACITY)
    live = store.create("live", lifetime_seconds=LIFETIME_SECONDS * SMALL_CAPACITY)
    expired = store.create("expired", lifetime_seconds=LIFETIME_SECONDS)
    now[0] += LIFETIME_SECONDS
    newest = store.create("newest", lifetime_seconds=LIFETIME_SECONDS)
    restarted = OidcSessionStore(path=session_path, clock=lambda: now[0])
    assert restarted.lookup(live) == "live"
    assert restarted.lookup(expired) is None
    assert restarted.lookup(newest) == "newest"


@pytest.mark.parametrize("content", [
    b"{broken", b"\xff", b"[]", b'{"version": 999, "sessions": {}}',
    b'{"version": 1, "sessions": {"raw-cookie": [{"subject": "operator"}, 9999]}}',
])
def test_corrupt_file_warns_and_allows_startup(session_path: Path, content: bytes) -> None:
    session_path.parent.mkdir()
    session_path.write_bytes(content)
    with capture_logs() as logs:
        store = OidcSessionStore(path=session_path)
    assert store.lookup("raw-cookie") is None
    assert any(entry["event"] == "dashboard_oidc.sessions_load_failed" for entry in logs)
    assert all(entry["log_level"] == "warning" for entry in logs)
    assert "raw-cookie" not in str(logs)
    cookie = store.create(IDENTITY, lifetime_seconds=LIFETIME_SECONDS)
    assert OidcSessionStore(path=session_path).lookup(cookie) == IDENTITY.subject


def test_invalid_entry_rejects_entire_file(session_path: Path) -> None:
    store = OidcSessionStore(path=session_path)
    cookie = store.create(IDENTITY, lifetime_seconds=LIFETIME_SECONDS)
    document = json.loads(session_path.read_text())
    document["sessions"][hashlib.sha256(b"invalid").hexdigest()] = [{"subject": "other"}, "infinite"]
    session_path.write_text(json.dumps(document))
    with capture_logs() as logs:
        restarted = OidcSessionStore(path=session_path)
    assert restarted.lookup(cookie) is None
    assert logs[0]["event"] == "dashboard_oidc.sessions_load_failed"


def test_unreadable_file_warns_and_allows_startup(session_path: Path) -> None:
    with (
        patch.object(Path, "read_bytes", side_effect=PermissionError("private-content")),
        capture_logs() as logs,
    ):
        store = OidcSessionStore(path=session_path)
    assert store.lookup("unknown") is None
    assert logs[0]["event"] == "dashboard_oidc.sessions_load_failed"
    assert "private-content" not in str(logs)


def test_missing_file_does_not_warn_or_write(session_path: Path) -> None:
    with capture_logs() as logs:
        store = OidcSessionStore(path=session_path)
    assert store.lookup("unknown") is None
    assert not session_path.exists()
    assert not logs


def test_private_permissions_on_create_and_replace(session_path: Path) -> None:
    store = OidcSessionStore(path=session_path)
    cookie = store.create(IDENTITY, lifetime_seconds=LIFETIME_SECONDS)
    assert stat.S_IMODE(session_path.stat().st_mode) == PRIVATE_MODE
    session_path.chmod(PUBLIC_MODE)
    store.revoke(cookie)
    assert stat.S_IMODE(session_path.stat().st_mode) == PRIVATE_MODE
    assert list(session_path.parent.iterdir()) == [session_path]


def test_disk_contains_hash_and_never_raw_cookie(session_path: Path) -> None:
    store = OidcSessionStore(path=session_path)
    cookie = store.create(IDENTITY, lifetime_seconds=LIFETIME_SECONDS)
    document = json.loads(session_path.read_text())
    assert list(document["sessions"]) == [hashlib.sha256(cookie.encode()).hexdigest()]
    for path in session_path.parent.iterdir():
        assert cookie not in path.read_text()
    assert store.lookup(hashlib.sha256(cookie.encode()).hexdigest()) is None


def test_successful_lookups_and_unknown_logout_do_not_write(session_path: Path) -> None:
    store = OidcSessionStore(path=session_path)
    cookie = store.create(IDENTITY, lifetime_seconds=LIFETIME_SECONDS)
    with patch("coordinare.dashboard_oidc.os.replace") as replace:
        assert OidcSessionStore(path=session_path).identity(cookie) == IDENTITY
        assert store.lookup(cookie) == IDENTITY.subject
        store.revoke("unknown")
    replace.assert_not_called()


def test_atomic_replace_failure_preserves_previous_file(session_path: Path) -> None:
    store = OidcSessionStore(path=session_path)
    cookie = store.create(IDENTITY, lifetime_seconds=LIFETIME_SECONDS)
    previous = session_path.read_bytes()
    with (
        patch("coordinare.dashboard_oidc.os.replace", side_effect=OSError("disk unavailable")),
        pytest.raises(OSError, match="disk unavailable"),
    ):
        store.revoke(cookie)
    assert session_path.read_bytes() == previous
    assert list(session_path.parent.iterdir()) == [session_path]


def test_failed_logout_can_be_retried_and_persisted(session_path: Path) -> None:
    store = OidcSessionStore(path=session_path)
    cookie = store.create(IDENTITY, lifetime_seconds=LIFETIME_SECONDS)
    with (
        patch("coordinare.dashboard_oidc.os.replace", side_effect=PermissionError),
        pytest.raises(PermissionError),
    ):
        store.revoke(cookie)
    store.revoke(cookie)
    assert OidcSessionStore(path=session_path).lookup(cookie) is None


def test_temporary_file_is_private_and_hashed_before_replace(session_path: Path) -> None:
    replace = os.replace
    cookie = "known-session-cookie"

    def check_replace(source: Path, destination: Path) -> None:
        assert source.parent == session_path.parent
        assert destination == session_path
        assert stat.S_IMODE(source.stat().st_mode) == PRIVATE_MODE
        assert cookie not in source.read_text()
        assert hashlib.sha256(cookie.encode()).hexdigest() in source.read_text()
        replace(source, destination)

    with (
        patch("coordinare.dashboard_oidc.secrets.token_urlsafe", return_value=cookie),
        patch("coordinare.dashboard_oidc.os.replace", side_effect=check_replace) as atomic_replace,
    ):
        OidcSessionStore(path=session_path).create(IDENTITY, lifetime_seconds=LIFETIME_SECONDS)
    atomic_replace.assert_called_once()


def test_startup_cleanup_write_failure_does_not_stop_startup(session_path: Path) -> None:
    store = OidcSessionStore(path=session_path, clock=lambda: START_TIME)
    cookie = store.create(IDENTITY, lifetime_seconds=LIFETIME_SECONDS)
    with patch("coordinare.dashboard_oidc.os.replace", side_effect=PermissionError):
        restarted = OidcSessionStore(
            path=session_path, clock=lambda: START_TIME + LIFETIME_SECONDS,
        )
    assert restarted.lookup(cookie) is None


def test_daemon_builds_persistent_flow_beside_expanded_state_file(
    tmp_path: Path,
) -> None:
    state_file = Path("~/state/daemon.json")
    with patch.object(Path, "expanduser", return_value=tmp_path / "state" / "daemon.json"):
        first = _build_dashboard_oidc_flow(_oidc_config(), state_file_path=state_file)
        cookie, _ = first.start_session(IDENTITY)
        second = _build_dashboard_oidc_flow(_oidc_config(), state_file_path=state_file)
    assert second.sessions.identity(cookie) == IDENTITY
    assert (tmp_path / "state" / "dashboard_oidc_sessions.json").exists()


def test_login_restart_and_logout_via_dashboard(session_path: Path) -> None:
    double = ProviderDouble()
    app = _app(double)
    app.state.oidc.sessions = OidcSessionStore(path=session_path)
    with TestClient(app, base_url=BASE) as client:
        _log_in(client, double)
        cookie = client.cookies.get(SESSION_COOKIE)
        subject = client.get("/api/account").json()["subject"]
    restarted_app = _app()
    restarted_app.state.oidc.sessions = OidcSessionStore(path=session_path)
    with TestClient(restarted_app, base_url=BASE) as client:
        client.cookies.set(SESSION_COOKIE, cookie)
        assert client.get("/api/account").json()["subject"] == subject
        response = client.post("/oidc/logout", headers={"Origin": BASE})
        assert response.status_code == httpx.codes.NO_CONTENT
    final_app = _app()
    final_app.state.oidc.sessions = OidcSessionStore(path=session_path)
    with TestClient(final_app, base_url=BASE) as client:
        client.cookies.set(SESSION_COOKIE, cookie)
        assert client.get("/", follow_redirects=False).status_code == httpx.codes.FOUND


def test_flow_accepts_session_path(session_path: Path) -> None:
    first = OidcFlow(_oidc_config(), session_path=session_path)
    cookie, _ = first.start_session(IDENTITY)
    assert OidcFlow(_oidc_config(), session_path=session_path).sessions.identity(cookie) == IDENTITY
