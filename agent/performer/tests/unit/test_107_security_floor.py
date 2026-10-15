from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from performer.backends.base import BackendStatus
from performer.config import Settings
from performer.main import handle_dispatch, handle_status
from performer.models import Score, Stand
from performer.protocol import PerformerMessage
from performer.security_floor import run_security_floor
from performer.security_scanner import ScannerError, scan_diff
from tests.unit.test_107_security_hardening import HEAD
from tests.unit import test_107_security_hardening

security_head_tree = test_107_security_hardening.head_tree


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_security_job_scans_in_own_clone_and_returns_report(security_head_tree, failed):
    tmp_path, tracked, _ = security_head_tree
    tracked["app.py"] = "print(1)\n"
    (tmp_path / "app.py").write_text("print(1)\n")
    backend = MagicMock()
    backend.start = AsyncMock()
    backend.get_status.return_value = BackendStatus(state="done", output=json.dumps({"passed": True, "findings": []}))
    stand = Stand(path=tmp_path, branch="feature")
    finding = {"severity": "high", "category": "injection", "description": "semgrep:rule",
               "file": "app.py", "line": 1, "routing": "implementer"}
    message = PerformerMessage(action="dispatch", payload={
        "title": "Card", "repo_url": "https://github.com/a/b", "branch": "feature", "role": "security",
        "security_floor": {"version": 1, "files": ["app.py"], "diff_status": "ok", "head_sha": HEAD},
    })
    with (
        patch("performer.main.clone_repository", new=AsyncMock(return_value=stand)),
        patch("performer.main.get_backend", return_value=backend),
        patch("performer.main.get_head_sha", new=AsyncMock(return_value="head")),
        patch("performer.main.commit_file", new=AsyncMock()) as commit,
        patch("performer.security_floor.scan_diff", side_effect=RuntimeError("scanner broken") if failed else None,
              return_value=[finding]) as scan,
    ):
        _, perf = await handle_dispatch(message, Settings(AGENT_BACKEND="codex"))
        assert scan.call_count == 1
        assert scan.call_args.args[0] == ["app.py"]
        assert not scan.call_args.args[1].is_relative_to(tmp_path)
        response = await handle_status(PerformerMessage(action="status", session_id=perf.session_id), perf)
        repeated = await handle_status(PerformerMessage(action="status", session_id=perf.session_id), perf)
        assert repeated.report == response.report
        assert scan.call_count == 1
    report = response.model_dump()["report"]["security_floor"]
    assert report["version"] == 1 and report["complete"] is (not failed)
    assert report["tools"] == ["semgrep", "bandit"]
    assert response.status == ("env_blocked" if failed else "security_failed")
    if not failed:
        assert report["findings"] == [finding]
        assert response.findings == [finding]
        assert "**Result: FAILED**" in commit.call_args.args[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("scan_request", [None, {}, {"version": 99},
    {"version": 1, "files": [], "diff_status": "failed"},
    {"version": 1, "files": ["../outside.py"], "diff_status": "ok"},
    {"version": 1, "files": ["missing.py"], "diff_status": "ok"},
    {"version": 1, "files": ["-config.py"], "diff_status": "ok"}])
async def test_invalid_or_old_dispatch_cannot_report_clean_scan(tmp_path, scan_request):
    score = Score(title="Card", repo_url="https://github.com/a/b", branch="b", security_floor=scan_request)
    with patch("performer.security_floor.scan_diff") as scan:
        report = await run_security_floor(score, Stand(path=tmp_path, branch="b"))
    assert report["complete"] is False
    scan.assert_not_called()


@pytest.mark.parametrize("stdout,code", [('{"results": [], "errors": []}', 2),
    ('{"results": [], "errors": [{"message": "scan failed"}]}', 0),
    ('{}', 0), ('[]', 0), ('{"results": "broken"}', 0)])
def test_scanner_failure_json_is_never_a_clean_scan(tmp_path, stdout, code):
    with patch("performer.security_scanner._resolve_binary", side_effect=lambda tool: f"/usr/bin/{tool}"), patch("performer.security_scanner.subprocess.run", return_value=MagicMock(stdout=stdout, returncode=code)):
        with pytest.raises(ScannerError):
            scan_diff(["app.py"], tmp_path)


@pytest.mark.asyncio
async def test_scan_paths_cannot_escape_workspace_through_symlink(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("print(1)\n")
    (root / "link.py").symlink_to(outside)
    score = Score(title="Card", repo_url="https://github.com/a/b", branch="b", security_floor={
        "version": 1, "files": ["link.py"], "diff_status": "ok",
    })
    with patch("performer.security_floor.scan_diff") as scan:
        report = await run_security_floor(score, Stand(path=root, branch="b"))
    assert report["complete"] is False
    scan.assert_not_called()


def test_scan_paths_starting_with_dash_are_operands(tmp_path):
    with patch("performer.security_scanner._resolve_binary", side_effect=lambda tool: f"/usr/bin/{tool}"), patch("performer.security_scanner.subprocess.run", return_value=MagicMock(
        stdout='{"results": [], "errors": []}', returncode=0,
    )) as run:
        assert scan_diff(["-config.py"], tmp_path) == []
    assert run.call_count == 2
    assert all(call.args[0][-1] == "./-config.py" for call in run.call_args_list)
