"""Spec 132: the dashboard serves a run's full log, behind the dashboard's authentication.

Paging returns text with the offsets in headers; ``download=1`` streams the whole file.
A run id is one safe path segment, so no request reaches a file outside the root.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.localhost_guard import build_permitted
from coordinare.mcp.tools import is_deferred
from coordinare.services.run_log import RootGuard, make_run_logs_collector

TOKEN = "a-secret-operator-token-of-at-least-32-characters"
BASE = "http://127.0.0.1:8090"
RUN = "run-132-route"
BODY = "".join(f"line {i:04d}\n" for i in range(1000)).encode()


def _collector(root: Path, *, enabled: bool = True) -> object:
    return make_run_logs_collector(
        enabled=enabled, root=root, max_bytes=1024 * 1024, retention_days=30, guard=RootGuard(),
    )


def _app(collector: object | None, token: str | None = None) -> object:
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0}
    if collector is not None:
        daemon.state["run_log_collector"] = collector
    return create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(),
        auth_token=SecretStr(token) if token is not None else None,
        permitted_origins=build_permitted(dashboard_host="127.0.0.1", dashboard_port=8090),
    )


def _write_log(root: Path, run_id: str = RUN, body: bytes = BODY) -> None:
    root.mkdir(parents=True, exist_ok=True)
    final = root / f"{run_id}.log"
    final.write_bytes(body)
    os.chmod(final, 0o600)


def test_the_log_is_refused_without_the_dashboard_token(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    _write_log(root)
    app = _app(_collector(root), token=TOKEN)
    with TestClient(app, base_url=BASE) as client:
        assert client.get(f"/api/runs/{RUN}/log").status_code == 401
        assert client.get(
            f"/api/runs/{RUN}/log", headers={"Authorization": "Bearer wrong"},
        ).status_code == 401
        ok = client.get(f"/api/runs/{RUN}/log", headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200


def test_a_page_of_the_log_is_text_with_its_offsets(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    _write_log(root)
    with TestClient(_app(_collector(root)), base_url=BASE) as client:
        first = client.get(f"/api/runs/{RUN}/log", params={"offset": 0, "limit": 100})
        assert first.status_code == 200
        assert first.headers["content-type"].startswith("text/plain")
        assert first.text == BODY[:100].decode()
        assert first.headers["x-log-size"] == str(len(BODY))
        assert first.headers["x-log-next-offset"] == "100"

        last = client.get(
            f"/api/runs/{RUN}/log", params={"offset": len(BODY) - 10, "limit": 1000},
        )
        assert last.text == BODY[-10:].decode()
        assert "x-log-next-offset" not in last.headers


def test_download_streams_the_whole_file_as_an_attachment(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    _write_log(root)
    with TestClient(_app(_collector(root)), base_url=BASE) as client:
        response = client.get(f"/api/runs/{RUN}/log", params={"download": "1"})

    assert response.status_code == 200
    assert response.content == BODY
    assert response.headers["content-disposition"] == f'attachment; filename="{RUN}.log"'


@pytest.mark.parametrize(
    "run_id",
    ["..%2F..%2Fetc%2Fpasswd", "%2E%2E", ".hidden", "a%5Cb", "unknown-run", "..%00"],
)
def test_an_unsafe_or_unknown_run_id_is_not_found(tmp_path: Path, run_id: str) -> None:
    root = tmp_path / "run-logs"
    _write_log(root)
    (tmp_path / "secret.log").write_text("not a run log\n")
    with TestClient(_app(_collector(root)), base_url=BASE) as client:
        response = client.get(f"/api/runs/{run_id}/log")
        download = client.get(f"/api/runs/{run_id}/log", params={"download": "1"})

    assert response.status_code == 404
    assert download.status_code == 404
    assert "not a run log" not in response.text


def test_a_linked_log_file_is_not_served(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private host file\n")
    os.symlink(outside, root / f"{RUN}.log")
    with TestClient(_app(_collector(root)), base_url=BASE) as client:
        response = client.get(f"/api/runs/{RUN}/log")
        download = client.get(f"/api/runs/{RUN}/log", params={"download": "1"})

    assert response.status_code == 404
    assert download.status_code == 404
    assert "private host file" not in response.text


def test_a_run_whose_log_was_not_collected_is_not_found(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    root.mkdir()
    with TestClient(_app(_collector(root)), base_url=BASE) as client:
        response = client.get(f"/api/runs/{RUN}/log")
    assert response.status_code == 404
    assert "no full log" in response.text.lower()


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 10_000_000}, {"offset": -1}])
def test_out_of_range_paging_is_rejected(tmp_path: Path, params: dict[str, int]) -> None:
    root = tmp_path / "run-logs"
    _write_log(root)
    with TestClient(_app(_collector(root)), base_url=BASE) as client:
        assert client.get(f"/api/runs/{RUN}/log", params=params).status_code == 422


def test_no_collector_means_no_logs(tmp_path: Path) -> None:
    with TestClient(_app(None), base_url=BASE) as client:
        assert client.get(f"/api/runs/{RUN}/log").status_code == 404


def test_a_disabled_collector_serves_nothing(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    _write_log(root)
    with TestClient(_app(_collector(root, enabled=False)), base_url=BASE) as client:
        assert client.get(f"/api/runs/{RUN}/log").status_code == 404


def test_the_run_log_route_is_deferred_from_the_mcp_tools() -> None:
    assert is_deferred("/api/runs/{run_id}/log")


def test_the_card_offers_the_full_log_only_once_it_is_committed(tmp_path: Path) -> None:
    from coordinare.dashboard.store import DashboardStore

    root = tmp_path / "run-logs"
    daemon = MagicMock()
    daemon.state = {"run_log_collector": _collector(root)}

    collector = daemon.state["run_log_collector"]
    assert DashboardStore._full_log_available(daemon, RUN) is False  # the run is still going
    _write_log(root)
    collector.mark_committed(RUN)  # the commit is recorded by the teardown, not read from disk
    assert DashboardStore._full_log_available(daemon, RUN) is True
    assert DashboardStore._full_log_available(daemon, None) is False
    assert DashboardStore._full_log_available(daemon, "../escape") is False
