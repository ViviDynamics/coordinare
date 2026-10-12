"""Unit tests for 553 — orphaned PR reconciliation for completed cards (issue #78)."""
from __future__ import annotations

from coordinare.config import OrphanPrConfig, ProjectConfiguration

_BASE = {
    "project_name": "Demo",
    "github_org": "acme",
    "github_project_number": 1,
    "github_token": "tok",
    "human_reviewers": ["alice"],
}


# ---------------------------------------------------------------------------
# Task 1 — config block: opt-in, default off, independent switches
# ---------------------------------------------------------------------------


def test_orphan_pr_config_defaults_off() -> None:
    cfg = ProjectConfiguration(**_BASE)
    assert isinstance(cfg.orphan_pr, OrphanPrConfig)
    assert cfg.orphan_pr.enabled is False
    assert cfg.orphan_pr.completion_reconciliation is False
    assert cfg.orphan_pr.sweep is False


def test_orphan_pr_config_independent_switches() -> None:
    cfg = ProjectConfiguration(
        **_BASE, orphan_pr=OrphanPrConfig(enabled=True, sweep=True)
    )
    assert cfg.orphan_pr.enabled is True
    assert cfg.orphan_pr.sweep is True
    assert cfg.orphan_pr.completion_reconciliation is False

    cfg2 = ProjectConfiguration(
        **_BASE,
        orphan_pr=OrphanPrConfig(enabled=True, completion_reconciliation=True),
    )
    assert cfg2.orphan_pr.completion_reconciliation is True
    assert cfg2.orphan_pr.sweep is False


def test_orphan_pr_config_rejects_unknown_keys() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ProjectConfiguration(
            **_BASE,
            orphan_pr=OrphanPrConfig(enabled=True, unknown_switch=True),  # type: ignore[arg-type]
        )
