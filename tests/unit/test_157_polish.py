"""#157: the small polish. The shared time helper and the persona line under node, the chip's identity claims, and the one-helper rule.

The browser behaviour of each item is in tests/e2e/test_157_polish.py.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coordinare.dashboard import _DASHBOARD_JS_SOURCES
from coordinare.dashboard.assets import _ACTIVITY_STREAM_JS
from tests.unit.dashboard_js_slices import shipped_function, time_helpers_js
from tests.unit.test_497_oidc_session import BASE, _app, _log_in
from tests.unit.test_554_dashboard_account import ProfileProvider

_CHECKS_JS = Path(__file__).resolve().parents[1] / "js" / "time_157_checks.js"
_EXPECTED_EXACT = {
    # The same instant, read in two zones: the zone label and the wall clock both change with TZ.
    "America/New_York": "2026-10-13 17:00:50 EDT",
    "UTC": "2026-10-13 21:00:50 UTC",
}


def _sliced_js(tmp_path: Path) -> Path:
    path = tmp_path / "time_157.js"
    path.write_text(
        "\n\n".join([
            shipped_function("helpers", "esc"),
            time_helpers_js(),
            shipped_function("app", "personaAuthorText"),
            shipped_function("app", "personaLastEditedHtml"),
        ]),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("zone", sorted(_EXPECTED_EXACT))
def test_time_helpers_under_node_in_each_zone(tmp_path: Path, zone: str) -> None:
    """The exact time carries its zone, the age is relative, and the fallbacks hold, in New York and in UTC."""
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    env = {**os.environ, "TZ": zone, "TIME_157_EXPECT": _EXPECTED_EXACT[zone]}
    result = subprocess.run(
        [node, str(_CHECKS_JS), str(_sliced_js(tmp_path))],
        capture_output=True, text=True, timeout=60, check=False, env=env,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side time checks failed in {zone}:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, report
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 20, report


def test_every_absolute_time_goes_through_timehtml() -> None:
    """One helper renders a time. The old local-only formatter is gone from every shipped script."""
    shipped = "\n".join([*_DASHBOARD_JS_SOURCES.values(), _ACTIVITY_STREAM_JS])
    assert "toLocaleTimeString" not in shipped
    assert not re.search(r"new Date\([^)]*\)\.toLocaleString", shipped)
    assert "afTime(" not in shipped
    assert "timeHtml(" in _ACTIVITY_STREAM_JS


def test_the_chip_reads_the_preferred_username_then_name_then_email() -> None:
    """The account route carries the login name and the provider the verified claims name."""

    class _Named(ProfileProvider):
        def id_token(self, **overrides: object) -> str:
            return super().id_token(preferred_username="akadmin", **overrides)

    provider = _Named()
    with TestClient(_app(provider), base_url=BASE) as client:
        _log_in(client, provider)
        profile = client.get("/api/account").json()
    assert profile["username"] == "akadmin"
    assert profile["name"] == "Sample Operator"
    assert profile["provider"] == "provider.test"


def test_the_account_chip_script_shows_the_username_with_the_full_name_in_the_title() -> None:
    account = _DASHBOARD_JS_SOURCES["account"]
    assert "profile.username || profile.name || profile.email" in account
    assert "summary').title = who" in account
    assert "(profile.provider ? ' (' + profile.provider + ')' : '')" in account
