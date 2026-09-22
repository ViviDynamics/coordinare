"""Tests for the 433 strict-mypy per-module allowlist ratchet."""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
SRC = PYPROJECT.parent / "src"


_SNAPSHOT: tuple[str, ...] = (
    "coordinare.auth",
    "coordinare.config_descriptors",
    "coordinare.daemon",
    "coordinare.dashboard",
    "coordinare.eval.implementer_scenarios",
    "coordinare.eval.qa_scenarios",
    "coordinare.graph.nodes.assess_card",
    "coordinare.graph.nodes.handle_blocked",
    "coordinare.protocol",
    "coordinare.services.config_write_service",
    "coordinare.services.kubernetes_runtime",
    "coordinare.services.rebase",
    "coordinare.services.reconciliation",
    "coordinare.services.security_scanner",
    "coordinare.session",
    "coordinare.state_store",
)

def _ratchet_overrides() -> list[str]:
    doc = tomllib.loads(PYPROJECT.read_text())
    overrides = doc["tool"]["mypy"]["overrides"]
    blocks = [
        o for o in overrides
        if o.get("ignore_errors") is True and "coordinare." in str(o.get("module", ""))
    ]
    assert len(blocks) == 1, "expected exactly one coordinare allowlist block"
    return blocks[0]["module"]


class TestAllowlist:
    def test_allowlist_is_sorted(self) -> None:
        modules = _ratchet_overrides()
        assert modules == sorted(modules)

    def test_allowlist_has_no_duplicates(self) -> None:
        modules = _ratchet_overrides()
        assert len(modules) == len(set(modules))

    def test_allowlist_matches_the_committed_snapshot(self) -> None:
        """The allowlist can only shrink: additions require touching this file."""
        assert tuple(_ratchet_overrides()) == _SNAPSHOT

    def test_every_entry_maps_to_a_real_file(self) -> None:
        for module in _ratchet_overrides():
            parts = module.split(".")
            assert parts[0] == "coordinare", module
            path = SRC.joinpath(*parts)
            assert path.with_suffix(".py").exists() or (path / "__init__.py").exists(), module

    def test_the_ratchet_tooth_is_ignore_errors_true(self) -> None:
        doc = tomllib.loads(PYPROJECT.read_text())
        strict = doc["tool"]["mypy"]["strict"]
        assert strict is True


def test_ci_detection_package_ships_py_typed() -> None:
    """The shim's import check depends on the marker staying shipped (PEP 561)."""
    marker = PYPROJECT.parent / "packages" / "ci_detection" / "src" / "coordinare_ci_detection" / "py.typed"
    assert marker.exists(), "py.typed must stay committed"
    pkg = tomllib.loads((marker.parents[2] / "pyproject.toml").read_text())
    assert pkg["tool"]["setuptools"]["package-data"]["coordinare_ci_detection"] == ["py.typed"]
