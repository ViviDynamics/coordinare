"""131: drift guard — the coordinare review-diff noise filter must stay consistent
with the canonical agent-config set owned by the performer package.

The two lists live in separate packages (coordinare keeps a literal to avoid a
runtime import of the performer package in the daemon). This test is the
mechanical link that makes them "one source of truth": it fails CI if a new agent
dir is added to `performer.noise_paths.AGENT_CONFIG_DIRS` but not mirrored into
the coordinare diff filter.
"""
from __future__ import annotations

from performer.noise_paths import AGENT_CONFIG_DIRS

from coordinare.graph.nodes.dispatch_performer import _DIFF_NOISE_PATH_MARKERS


def test_every_canonical_agent_dir_is_in_the_diff_filter() -> None:
    missing = [d for d in AGENT_CONFIG_DIRS if f"{d}/" not in _DIFF_NOISE_PATH_MARKERS]
    assert not missing, (
        "coordinare _DIFF_NOISE_PATH_MARKERS is missing agent dirs from the canonical "
        f"performer.noise_paths.AGENT_CONFIG_DIRS: {missing}. Add them as '<dir>/'."
    )


def test_diff_filter_markers_are_dir_anchored() -> None:
    # Agent-dir markers use a trailing slash so they match a directory path.
    for d in AGENT_CONFIG_DIRS:
        assert f"{d}/" in _DIFF_NOISE_PATH_MARKERS
