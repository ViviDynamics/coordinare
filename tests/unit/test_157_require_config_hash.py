"""Spec 157 — the global config write requires a version.

Specs 155 and 156 left `expected_hash` optional and logged the writes that arrived
without one, because refusing them is a contract change nobody could weigh without
knowing how often they happened. Every first-party caller now sends a version, so
what remained was automation writing config with no protection against overwriting
a concurrent edit.

The interesting cases are not "omitted" — they are the values that are *present but
useless*. A check written as `if expected_hash:` would have let an empty string
through as guarded when it guards nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from coordinare.config import CoordinareConfiguration
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.dashboard import DashboardStore, create_dashboard_app


def _client(config_path: Path) -> TestClient:
    raw = yaml.safe_load(config_path.read_text())
    raw["github_token"] = "ghp_fixturetoken"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(raw))
    daemon = MagicMock()
    daemon.state = {"coordinare_config": cfg, "config_version": 7}
    daemon.running = True
    app = create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(), config_path=config_path,
    )
    return TestClient(app, base_url="http://127.0.0.1:8090")


class TestNoShapeOfMissingVersionGetsThrough:
    """SC-001 — asserted across the values a caller might actually send."""

    #: (label, value, expected status). ``None`` for the value means "omit the key".
    #: Empty string and bare quotes are the ones worth having: they are *present*,
    #: so a truthiness check would call them supplied, and they guard nothing — 409
    #: is right, because they are a version that does not match.
    CASES: ClassVar[list] = [
        ("omitted", None, 428),
        ("null", "NULL", 428),
        ("empty string", "", 409),
        ("just quotes", '""', 409),
        ("zero", 0, 400),
        ("false", False, 400),
        ("a list", [], 400),
    ]

    @pytest.mark.parametrize(("label", "value", "expected"), CASES)
    def test_it_is_refused_and_the_file_is_untouched(
        self, temp_config_path, label, value, expected,
    ) -> None:
        client = _client(temp_config_path)
        body: dict = {"max_concurrent_cards": 4}
        if value is not None:
            body["expected_hash"] = None if value == "NULL" else value

        before = temp_config_path.read_bytes()
        resp = client.put("/api/config/global", json=body)

        assert resp.status_code == expected, f"{label}: {resp.text}"
        assert temp_config_path.read_bytes() == before, (
            f"{label} was refused but the file changed anyway"
        )


class TestTheRefusalTellsYouWhatToDo:
    """FR-003 — 'precondition failed' with no remedy is a worse error than none."""

    def test_it_names_the_field_and_where_to_get_it(self, temp_config_path) -> None:
        client = _client(temp_config_path)

        body = client.put("/api/config/global", json={"max_concurrent_cards": 4}).json()

        assert body["precondition_required"] is True
        assert "expected_hash" in body["error"]
        assert "ETag" in body["error"], "say where the version comes from"


class TestTheOtherOutcomesAreUnchanged:
    """FR-007 — tightening the missing case must not disturb the others."""

    def test_a_stale_version_is_still_409(self, temp_config_path) -> None:
        client = _client(temp_config_path)
        stale = client.get("/api/config/global").headers["etag"]
        temp_config_path.write_text(temp_config_path.read_text() + "\n# meanwhile\n")

        resp = client.put(
            "/api/config/global",
            json={"max_concurrent_cards": 4, "expected_hash": stale},
        )

        assert resp.status_code == 409, "a stale version must not be reported as missing"
        assert "# meanwhile" in temp_config_path.read_text()

    def test_a_current_version_still_writes_and_returns_the_next_one(
        self, temp_config_path,
    ) -> None:
        from coordinare.services.config_write_service import compute_content_hash

        client = _client(temp_config_path)
        current = client.get("/api/config/global").headers["etag"]

        body = client.put(
            "/api/config/global",
            json={"max_concurrent_cards": 4, "expected_hash": current},
        ).json()

        assert body["status"] == "saved"
        assert body["new_hash"] == compute_content_hash(temp_config_path)
        assert yaml.safe_load(temp_config_path.read_text())["max_concurrent_cards"] == 4


class TestThePageNeverSendsAVersionlessWrite:
    """SC-003 — the page fails closed rather than collecting a 428."""

    def test_it_refuses_locally_when_it_has_no_version(self) -> None:
        from coordinare.dashboard import _DASHBOARD_HTML

        assert "this page did not load a configuration version" in _DASHBOARD_HTML
        assert "payload.expected_hash = _adminCfgHash;" in _DASHBOARD_HTML

    def test_it_does_not_send_conditionally_any_more(self) -> None:
        """The old `if (_adminCfgHash) payload.expected_hash = ...` sent nothing when
        it had nothing, which is now exactly the refused case."""
        from coordinare.dashboard import _DASHBOARD_HTML

        assert "if (_adminCfgHash) payload.expected_hash" not in _DASHBOARD_HTML
