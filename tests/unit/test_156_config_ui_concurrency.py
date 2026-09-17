"""Spec 156 / issue #237 — the Global Config page stops overwriting concurrent edits.

`config_write_service` has had optimistic concurrency since spec 081, and the
catalog and routing endpoints use it. `PUT /api/config/global` never did, so two
operators with the dashboard open could silently overwrite each other while the
same collision on a catalog entry was correctly refused.

Spec 155 gave the endpoint an optional `expected_hash` for the config assistant.
The obstacle to using it from the older page was that the page had no way to
obtain a hash: its load endpoint returns values only, the hash lives in a far
heavier call, and the values response body is pinned by an existing test to an
exact key set — correctly, because that is a values contract. Hence an ETag.
"""

from __future__ import annotations

from pathlib import Path
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


class TestThePageCanObtainAHashCheaply:
    """US2 — US1 is not implementable without this, and the obvious routes are barred."""

    def test_the_values_response_carries_an_etag(self, temp_config_path) -> None:
        client = _client(temp_config_path)

        resp = client.get("/api/config/global")

        assert resp.status_code == 200
        assert resp.headers.get("etag"), "the page has no way to save safely without this"

    def test_the_etag_is_the_files_content_hash_and_is_a_valid_etag(
        self, temp_config_path,
    ) -> None:
        """Review finding: the first version of this test stripped quotes.

        ``etag.strip('"')`` is a no-op on an unquoted value, so the assertion passed
        whether or not the header was well-formed — it accommodated a mismatch
        instead of catching one. RFC 7232 §2.3 wants a DQUOTE-enclosed opaque tag,
        so that is what is asserted.
        """
        from coordinare.services.config_write_service import compute_content_hash

        client = _client(temp_config_path)

        etag = client.get("/api/config/global").headers["etag"]

        assert etag.startswith('"') and etag.endswith('"'), (
            f"{etag!r} is not a well-formed entity-tag; it works for our own string "
            "comparison and is still a malformed header"
        )
        assert etag[1:-1] == compute_content_hash(temp_config_path)

    def test_the_body_is_unchanged(self, temp_config_path) -> None:
        """SC-002 / FR-002 — an existing test pins this key set, and it is right to."""
        client = _client(temp_config_path)

        body = client.get("/api/config/global").json()

        assert "etag" not in body
        assert "expected_hash" not in body
        assert all(not k.startswith("_") for k in body), (
            "metadata leaked into a values contract"
        )

    def test_the_etag_changes_when_the_file_does(self, temp_config_path) -> None:
        client = _client(temp_config_path)
        before = client.get("/api/config/global").headers["etag"]

        temp_config_path.write_text(temp_config_path.read_text() + "\n# edited\n")

        assert client.get("/api/config/global").headers["etag"] != before


class TestAConcurrentEditIsRefusedRatherThanLost:
    """US1 — the defect."""

    def test_a_stale_save_is_refused_and_the_other_edit_survives(self, temp_config_path) -> None:
        client = _client(temp_config_path)
        stale = client.get("/api/config/global").headers["etag"]

        # A colleague saves while this page sits open.
        text = temp_config_path.read_text()
        temp_config_path.write_text(text + "\n# a colleague's edit\n")

        resp = client.put(
            "/api/config/global",
            json={"max_concurrent_cards": 4, "expected_hash": stale},
        )

        assert resp.status_code == 409
        assert "# a colleague's edit" in temp_config_path.read_text(), (
            "the concurrent edit was overwritten, which is the whole defect"
        )

    def test_a_current_save_still_works(self, temp_config_path) -> None:
        client = _client(temp_config_path)
        current = client.get("/api/config/global").headers["etag"]

        resp = client.put(
            "/api/config/global",
            json={"max_concurrent_cards": 4, "expected_hash": current},
        )

        assert resp.status_code == 200, resp.text
        assert yaml.safe_load(temp_config_path.read_text())["max_concurrent_cards"] == 4

    def test_the_page_refuses_to_save_without_a_version(self) -> None:
        """157: the page fails closed rather than collecting a 428 from the server."""
        from coordinare.dashboard import _DASHBOARD_HTML

        assert "this page did not load a configuration version" in _DASHBOARD_HTML

    def test_the_page_sends_the_hash_it_loaded_with(self) -> None:
        """FR-003, asserted on the page's own code.

        A round trip through a browser is not available here, and the failure this
        guards against is silent: the page would keep working, just unguarded.
        """
        from coordinare.dashboard import _DASHBOARD_HTML

        page = _DASHBOARD_HTML
        assert "_adminCfgHash = res.headers.get('ETag')" in page, (
            "the page never captures the version it is editing"
        )
        assert "payload.expected_hash = _adminCfgHash;" in page, (
            "the page captures a hash and then does not send it"
        )

    def test_the_page_explains_a_refusal(self ) -> None:
        """FR-005 — a raw 409 is not an explanation."""
        from coordinare.dashboard import _DASHBOARD_HTML

        assert "the configuration changed since this page loaded" in _DASHBOARD_HTML


class TestTheRemainingGapIsVisible:
    """FR-006 / SC-003 — still accepted, no longer silent."""

    @staticmethod
    def _events(logs):
        return [entry.get("event") for entry in logs]

    def test_an_unguarded_write_is_now_refused_and_logged(self, temp_config_path) -> None:
        """Spec 157 answered the contract question this test was posed to inform.

        156 accepted these and logged them, because refusing is a contract change
        and nobody could weigh it without knowing how often they happened. The answer
        was to refuse: silently losing an edit is worse than a loud failure a script
        can be taught to handle.
        """
        from structlog.testing import capture_logs

        client = _client(temp_config_path)

        with capture_logs() as logs:
            resp = client.put("/api/config/global", json={"max_concurrent_cards": 4})

        assert resp.status_code == 428
        assert resp.json()["precondition_required"] is True
        assert "expected_hash" in resp.json()["error"], (
            "a refusal should say what to send, not only that something was wrong"
        )
        assert "config.global_write_refused_unguarded" in self._events(logs)

    def test_a_guarded_write_is_not_logged(self, temp_config_path) -> None:
        from structlog.testing import capture_logs

        client = _client(temp_config_path)
        current = client.get("/api/config/global").headers["etag"]

        with capture_logs() as logs:
            client.put(
                "/api/config/global",
                json={"max_concurrent_cards": 4, "expected_hash": current},
            )

        assert "config.global_write_unguarded" not in self._events(logs)


@pytest.mark.parametrize("missing", [True, False])
def test_the_endpoint_still_works_without_a_config_file(tmp_path, missing) -> None:
    """Edge case: no file must not now fail differently because of the ETag."""
    config_path = tmp_path / "config.yaml"
    if missing:
        daemon = MagicMock()
        daemon.state = {"coordinare_config": None, "config": None}
        app = create_dashboard_app(
            DashboardStore(), daemon, MagicMock(), MagicMock(), config_path=config_path,
        )
        client = TestClient(app, base_url="http://127.0.0.1:8090")
        resp = client.get("/api/config/global")
        assert resp.status_code == 500
        assert "etag" not in resp.headers


class TestTheGuardHoldsForEverySaveNotJustTheFirst:
    """Found by my own review prompt before the review returned.

    Setting the page's hash to null after a successful save would have made the
    guard hold exactly once per page load — and that is worse than not having it,
    because it looks like it holds always. The endpoint hands back the new version,
    the way the catalog endpoints already do.
    """

    def test_the_put_returns_the_new_hash(self, temp_config_path) -> None:
        from coordinare.services.config_write_service import compute_content_hash

        client = _client(temp_config_path)
        current = client.get("/api/config/global").headers["etag"]

        body = client.put(
            "/api/config/global",
            json={"max_concurrent_cards": 4, "expected_hash": current},
        ).json()

        assert body["new_hash"] == compute_content_hash(temp_config_path)
        assert body["new_hash"] != current, "the file changed; so must its hash"

    def test_the_returned_hash_guards_the_next_save(self, temp_config_path) -> None:
        """The whole point: two saves in a row, both guarded, no reload in between."""
        client = _client(temp_config_path)
        first = client.get("/api/config/global").headers["etag"]

        second = client.put(
            "/api/config/global",
            json={"max_concurrent_cards": 4, "expected_hash": first},
        ).json()["new_hash"]

        # A colleague edits between the two saves.
        temp_config_path.write_text(temp_config_path.read_text() + "\n# meanwhile\n")

        resp = client.put(
            "/api/config/global",
            json={"max_concurrent_cards": 5, "expected_hash": second},
        )

        assert resp.status_code == 409, "the second save was unguarded"
        assert "# meanwhile" in temp_config_path.read_text()

    def test_the_page_carries_the_new_hash_forward(self) -> None:
        from coordinare.dashboard import _DASHBOARD_HTML

        assert "_adminCfgHash = d.new_hash || null;" in _DASHBOARD_HTML, (
            "the page drops its baseline after saving, so its next save is unguarded"
        )

    def test_the_status_key_is_unchanged_for_existing_callers(self, temp_config_path) -> None:
        """Adding a key is safe; changing one is not. Existing tests read `status`."""
        client = _client(temp_config_path)
        current = client.get("/api/config/global").headers["etag"]

        body = client.put(
            "/api/config/global",
            json={"max_concurrent_cards": 4, "expected_hash": current},
        ).json()

        assert body["status"] == "saved"


class TestBothFormsOfTheHashAreAccepted:
    """Quoting the header correctly would otherwise have 409'd every save.

    A client that reads the version from the ``ETag`` sends it back with quotes; one
    holding it from ``new_hash`` sends it bare. They are the same version.
    """

    def test_a_quoted_hash_is_accepted(self, temp_config_path) -> None:
        client = _client(temp_config_path)
        quoted = client.get("/api/config/global").headers["etag"]

        assert quoted.startswith('"')
        resp = client.put(
            "/api/config/global",
            json={"max_concurrent_cards": 4, "expected_hash": quoted},
        )

        assert resp.status_code == 200, resp.text

    def test_a_bare_hash_is_accepted(self, temp_config_path) -> None:
        """The config assistant holds a bare hash from /api/config/all."""
        from coordinare.services.config_write_service import compute_content_hash

        client = _client(temp_config_path)

        resp = client.put(
            "/api/config/global",
            json={
                "max_concurrent_cards": 4,
                "expected_hash": compute_content_hash(temp_config_path),
            },
        )

        assert resp.status_code == 200, resp.text

    def test_a_quoted_stale_hash_is_still_refused(self, temp_config_path) -> None:
        """Tolerating the quotes must not tolerate the staleness."""
        client = _client(temp_config_path)
        stale = client.get("/api/config/global").headers["etag"]
        temp_config_path.write_text(temp_config_path.read_text() + "\n# meanwhile\n")

        resp = client.put(
            "/api/config/global",
            json={"max_concurrent_cards": 4, "expected_hash": stale},
        )

        assert resp.status_code == 409
