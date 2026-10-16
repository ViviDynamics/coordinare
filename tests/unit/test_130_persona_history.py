"""Spec 130: persona version history.

Covers the append-only store (first version, no-op on unchanged text, survives a
restart, cap, torn line), the dispatch-time reference (a hand edit becomes a version
named "unknown"), the dashboard recording points (PUT and DELETE, actor from the OIDC
session or "local"), the read API (list, one version with its diff, 404s, auth gate),
and the MCP deferral of the new routes.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coordinare.config import DashboardOidcConfig
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.dashboard_oidc import OidcFlow, OidcIdentity
from coordinare.localhost_guard import build_permitted
from coordinare.mcp.tools import is_deferred
from coordinare.services import persona_history as ph
from coordinare.services.persona_history import (
    LOCAL_ACTOR,
    PERSONA_HISTORY_MAX_VERSIONS,
    UNKNOWN_ACTOR,
    PersonaHistory,
    persona_history_root,
    reference_for_dispatch,
)

BASE = "http://127.0.0.1:8090"
TOKEN = "t" * 40


def _history(root: Path, max_versions: int = PERSONA_HISTORY_MAX_VERSIONS) -> PersonaHistory:
    return PersonaHistory(root, max_versions=max_versions)


# --- store ------------------------------------------------------------------


def test_the_cap_default_is_the_named_constant_of_200() -> None:
    assert PERSONA_HISTORY_MAX_VERSIONS == 200


def test_first_record_creates_version_one(tmp_path: Path) -> None:
    store = _history(tmp_path / "h")
    version = store.record("implementer", "Be precise.", changed_by="Ada", source="dashboard")
    assert version is not None
    assert version.version == 1
    assert version.text == "Be precise."
    assert version.changed_by == "Ada"
    assert version.source == "dashboard"
    assert len(version.sha256) == 64
    assert store.latest("implementer") == version


def test_identical_text_creates_no_new_version(tmp_path: Path) -> None:
    store = _history(tmp_path / "h")
    store.record("implementer", "same", changed_by="Ada", source="dashboard")
    assert store.record("implementer", "same", changed_by="Bob", source="dashboard") is None
    assert [v.version for v in store.versions("implementer")] == [1]


def test_an_edit_creates_the_next_version_and_keeps_the_old_text(tmp_path: Path) -> None:
    store = _history(tmp_path / "h")
    store.record("implementer", "first text", changed_by="Ada", source="dashboard")
    store.record("implementer", "second text", changed_by="Bob", source="dashboard")
    first = store.get("implementer", 1)
    second = store.get("implementer", 2)
    latest = store.latest("implementer")
    assert first is not None and first.text == "first text"
    assert second is not None and second.text == "second text"
    assert latest is not None and latest.version == 2


def test_versions_survive_a_restart_and_numbering_continues(tmp_path: Path) -> None:
    root = tmp_path / "h"
    _history(root).record("implementer", "one", changed_by="Ada", source="dashboard")
    _history(root).record("implementer", "two", changed_by="Ada", source="dashboard")
    restarted = _history(root)
    third = restarted.record("implementer", "three", changed_by="Ada", source="dashboard")
    assert third is not None and third.version == 3
    assert [v.text for v in _history(root).versions("implementer")] == ["one", "two", "three"]


def test_the_cap_prunes_the_oldest_versions(tmp_path: Path) -> None:
    root = tmp_path / "h"
    store = _history(root, max_versions=3)
    for index in range(1, 6):
        store.record("implementer", f"text {index}", changed_by="Ada", source="dashboard")
    assert [v.version for v in store.versions("implementer")] == [3, 4, 5]
    assert store.get("implementer", 1) is None
    assert len((root / "implementer.jsonl").read_text().splitlines()) == 3
    assert [v.version for v in _history(root, max_versions=3).versions("implementer")] == [3, 4, 5]


def test_a_torn_last_line_is_skipped_and_the_next_record_starts_clean(tmp_path: Path) -> None:
    root = tmp_path / "h"
    store = _history(root)
    store.record("implementer", "good", changed_by="Ada", source="dashboard")
    with (root / "implementer.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"version": 2, "text": "half')
    restarted = _history(root)
    assert [v.version for v in restarted.versions("implementer")] == [1]
    assert restarted.record("implementer", "after the tear", changed_by="Ada", source="dashboard") is not None
    assert [v.text for v in _history(root).versions("implementer")] == ["good", "after the tear"]
    # The torn fragment is closed off on its own line, so the records after it parse whole.
    whole: list[str] = []
    for line in (root / "implementer.jsonl").read_text().splitlines():
        try:
            whole.append(json.loads(line)["text"])
        except ValueError:
            continue
    assert whole == ["good", "after the tear"]


def test_an_unsafe_role_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _history(tmp_path / "h").record("../escape", "x", changed_by="Ada", source="dashboard")


def test_the_store_writes_nothing_outside_its_root(tmp_path: Path) -> None:
    root = tmp_path / "h"
    _history(root).record("implementer", "x", changed_by="Ada", source="dashboard")
    assert sorted(p.name for p in root.iterdir()) == ["implementer.jsonl"]
    assert not (tmp_path / "escape.jsonl").exists()


# --- dispatch reference -----------------------------------------------------


def test_dispatch_records_the_first_seen_text_as_unknown(tmp_path: Path) -> None:
    ref = reference_for_dispatch(tmp_path / "h", "implementer", "code default text")
    assert ref["version"] == 1
    assert ref["source"] == "first_seen"
    assert ref["changed_by"] == UNKNOWN_ACTOR
    assert ref["text"] == "code default text"
    assert len(ref["version_sha256"]) == 64


def test_a_hand_edit_found_at_dispatch_becomes_a_version(tmp_path: Path) -> None:
    root = tmp_path / "h"
    _history(root).record("implementer", "as dashboard saved", changed_by="Ada", source="dashboard")
    ref = reference_for_dispatch(root, "implementer", "edited in the yaml file")
    assert ref["version"] == 2
    assert ref["source"] == "outside_dashboard"
    assert ref["changed_by"] == UNKNOWN_ACTOR


def test_an_unchanged_text_at_dispatch_keeps_the_existing_version(tmp_path: Path) -> None:
    root = tmp_path / "h"
    _history(root).record("implementer", "saved", changed_by="Ada", source="dashboard")
    ref = reference_for_dispatch(root, "implementer", "saved")
    assert ref["version"] == 1
    assert ref["changed_by"] == "Ada"


def test_dispatch_without_a_root_says_why(tmp_path: Path) -> None:
    ref = reference_for_dispatch(None, "implementer", "text")
    assert ref["version"] is None
    assert ref["reason"]


def test_history_root_sits_next_to_the_state_snapshot(tmp_path: Path) -> None:
    config = SimpleNamespace(state_file_path=tmp_path / "state" / "coordinare.state.json")
    assert persona_history_root(config) == tmp_path / "state" / "persona_history"
    assert persona_history_root(None) is None


def test_persona_text_hash_is_stable() -> None:
    assert ph.persona_text_sha256("abc") == ph.persona_text_sha256("abc")
    assert ph.persona_text_sha256("abc") != ph.persona_text_sha256("abd")


# --- dashboard: recording points and read API -------------------------------


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text("personas: {}\n", encoding="utf-8")
    return path


def _client(tmp_path: Path, *, oidc: OidcFlow | None = None, token: str | None = None) -> TestClient:
    daemon = MagicMock()
    daemon.state = {
        "startup_complete": True,
        "config": SimpleNamespace(state_file_path=tmp_path / "state" / "coordinare.state.json"),
    }
    daemon.state_store.last_snapshot = None
    app = create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(),
        config_path=_config_file(tmp_path),
        permitted_origins=build_permitted(dashboard_host="127.0.0.1", dashboard_port=8090),
        oidc=oidc,
        auth_token=SecretStr(token) if token is not None else None,
    )
    return TestClient(app, base_url=BASE)


def _version_header(client: TestClient) -> str:
    response = client.get("/api/personas")
    return response.headers["ETag"]


def test_saving_a_persona_records_a_version_with_the_local_actor(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.put(
        "/api/personas/implementer",
        json={"instructions": "Write small diffs."},
        headers={"If-Match": _version_header(client)},
    )
    assert response.status_code == 200, response.text
    history = client.get("/api/personas/implementer/history").json()
    assert [v["version"] for v in history["versions"]] == [1]
    assert history["versions"][0]["changed_by"] == LOCAL_ACTOR
    assert history["versions"][0]["source"] == "dashboard"
    one = client.get("/api/personas/implementer/history/1").json()
    assert one["text"] == "Write small diffs."


def test_an_edit_keeps_the_old_text_readable_with_a_diff(tmp_path: Path) -> None:
    client = _client(tmp_path)
    for text in ("alpha line", "beta line"):
        response = client.put(
            "/api/personas/implementer",
            json={"instructions": text},
            headers={"If-Match": _version_header(client)},
        )
        assert response.status_code == 200, response.text
    assert client.get("/api/personas/implementer/history/1").json()["text"] == "alpha line"
    second = client.get("/api/personas/implementer/history/2").json()
    assert second["text"] == "beta line"
    assert second["previous_version"] == 1
    assert "-alpha line" in second["diff"]
    assert "+beta line" in second["diff"]


def test_reset_records_a_version_too(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.put(
        "/api/personas/implementer",
        json={"instructions": "custom"},
        headers={"If-Match": _version_header(client)},
    )
    response = client.delete(
        "/api/personas/implementer", headers={"If-Match": _version_header(client)},
    )
    assert response.status_code == 204, response.text
    versions = client.get("/api/personas/implementer/history").json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[1]["source"] == "dashboard_reset"


def test_the_actor_is_the_oidc_session_identity_when_oidc_is_on(tmp_path: Path) -> None:
    flow = OidcFlow(
        DashboardOidcConfig(
            discovery_url="https://issuer.example/.well-known/openid-configuration",
            client_id="cid",
            client_secret=SecretStr("csecret"),
            redirect_url="https://dashboard.example/oidc/callback",
        ),
    )
    value, _lifetime = flow.start_session(OidcIdentity(subject="sub-1", name="Ada Lovelace"))
    client = _client(tmp_path, oidc=flow)
    client.cookies.set("coordinare_session", value)
    client.put(
        "/api/personas/implementer",
        json={"instructions": "signed in edit"},
        headers={"If-Match": _version_header(client)},
    )
    versions = client.get("/api/personas/implementer/history").json()["versions"]
    assert versions[-1]["changed_by"] == "Ada Lovelace"


def test_history_routes_are_refused_without_the_token(tmp_path: Path) -> None:
    client = _client(tmp_path, token=TOKEN)
    assert client.get("/api/personas/implementer/history").status_code == 401
    assert client.get("/api/personas/implementer/history/1").status_code == 401
    authed = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/api/personas/implementer/history", headers=authed).status_code == 200


def test_unknown_role_and_unknown_version_are_404(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/api/personas/wizard/history").status_code == 404
    assert client.get("/api/personas/implementer/history/99").status_code == 404


def test_a_pruned_version_is_404_but_the_store_keeps_the_rest(tmp_path: Path) -> None:
    client = _client(tmp_path)
    store = _history(tmp_path / "state" / "persona_history", max_versions=2)
    for index in range(1, 4):
        store.record("implementer", f"v{index}", changed_by="Ada", source="dashboard")
    assert client.get("/api/personas/implementer/history/1").status_code == 404
    assert client.get("/api/personas/implementer/history/3").status_code == 200


def test_the_history_routes_are_deferred_from_mcp() -> None:
    assert is_deferred("/api/personas/{role}/history")
    assert is_deferred("/api/personas/{role}/history/{version}")
