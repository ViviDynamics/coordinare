"""Spec 132 / issue #180 — onboarding config hardening.

Covers three first-run walls:
  US1  performer entry point is portable (agent_executable + bin/performer)
  US2  example notifications ship off-by-default and validate without secrets
  US3  a symphony paused via ``enabled: false`` is announced once at startup
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_EXAMPLE = REPO_ROOT / "config.example.yaml"


def _load_example_raw() -> dict:
    return yaml.safe_load(CONFIG_EXAMPLE.read_text())


# ---------------------------------------------------------------------------
# US1 — performer launches from an unedited example (FR-001/002/003)
# ---------------------------------------------------------------------------


def test_example_agent_executable_points_at_portable_wrapper() -> None:
    raw = _load_example_raw()
    assert raw["agent_executable"] == "bin/run-performer"
    wrapper = REPO_ROOT / "bin" / "run-performer"
    assert wrapper.is_file()
    assert os.access(wrapper, os.X_OK)


def test_no_stale_performer_path_and_commented_ref_is_consistent() -> None:
    text = CONFIG_EXAMPLE.read_text()
    # No stale /usr/local/bin/performer references remain (active or commented).
    assert "/usr/local/bin/performer" not in text
    # The commented example reference points at the portable wrapper.
    assert 'agent_executable: "bin/run-performer"' in text


def test_bin_performer_is_portable_delegator() -> None:
    perf = REPO_ROOT / "bin" / "performer"
    assert perf.is_file()
    body = perf.read_text()
    # No developer-specific absolute home path.
    assert "/Users/" not in body
    # Delegates to the canonical wrapper rather than duplicating launch logic.
    assert "run-performer" in body
    assert "-m performer" not in body  # no duplicated `python -m performer` launch


# ---------------------------------------------------------------------------
# US2 — example notifications validate without secrets (FR-004/005)
# ---------------------------------------------------------------------------


def test_example_notifications_ship_off_by_default() -> None:
    notif = _load_example_raw()["notifications"]
    assert notif["channels"] == []
    assert notif["routing"] == []


def test_example_notifications_validate_without_secrets(monkeypatch) -> None:
    monkeypatch.delenv("COORDINARE_SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("COORDINARE_SMTP_PASSWORD", raising=False)
    from coordinare.config import NotificationsConfig

    # Building the shipped notifications block must not raise with no secrets set.
    NotificationsConfig(**_load_example_raw()["notifications"])


def test_example_retains_commented_channel_references() -> None:
    text = CONFIG_EXAMPLE.read_text()
    # Slack + email setup stays discoverable as commented reference.
    assert "slack-ops" in text
    assert "email-team" in text
    assert "smtp.mailgun.org" in text


# ---------------------------------------------------------------------------
# US3 — a paused symphony is announced once at startup (FR-006/007, SC-006)
# ---------------------------------------------------------------------------


def _make_daemon():
    from unittest.mock import AsyncMock, MagicMock

    from coordinare.daemon import CoordinareDaemon

    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={})
    return CoordinareDaemon(graph, poll_interval_seconds=1, max_cycles=1)


def _sym(name: str, enabled: bool):
    from coordinare.config import SymphonyConfig

    return SymphonyConfig(name=name, github_project_number=1, enabled=enabled)


def _paused_lines(logs: list[dict]) -> list[dict]:
    return [
        e
        for e in logs
        if "paused" in (str(e.get("event", "")) + str(e.get("detail", "")))
    ]


def test_disabled_symphony_announced_once_at_startup() -> None:
    import structlog.testing

    daemon = _make_daemon()
    daemon.state["symphony_configs"] = {
        "alpha": _sym("alpha", enabled=True),
        "beta": _sym("beta", enabled=False),
    }
    with structlog.testing.capture_logs() as logs:
        daemon._announce_paused_symphonies()

    paused = _paused_lines(logs)
    assert len(paused) == 1
    entry = paused[0]
    assert "beta" in (str(entry.get("symphony", "")) + str(entry.get("detail", "")))
    assert "enabled: false" in str(entry.get("detail", ""))


def test_all_enabled_symphonies_emit_no_paused_line() -> None:
    import structlog.testing

    daemon = _make_daemon()
    daemon.state["symphony_configs"] = {"alpha": _sym("alpha", enabled=True)}
    with structlog.testing.capture_logs() as logs:
        daemon._announce_paused_symphonies()

    assert _paused_lines(logs) == []


def test_no_per_cycle_disabled_symphony_logging() -> None:
    # SC-006: the paused state must not be logged on every poll cycle.
    daemon_src = (REPO_ROOT / "src" / "coordinare" / "daemon.py").read_text()
    assert "symphony.disabled_skip" not in daemon_src
