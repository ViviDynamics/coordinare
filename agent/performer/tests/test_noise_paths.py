"""131: canonical agent-noise path set + matcher (single source of truth)."""
from __future__ import annotations

import pytest

from performer.noise_paths import (
    AGENT_CONFIG_DIRS,
    exclude_globs,
    path_has_agent_config,
)


def test_all_supported_backend_dirs_present() -> None:
    for name in (".codex", ".claude", ".hermes", ".junie", ".opencode", ".openclaw", ".pi"):
        assert name in AGENT_CONFIG_DIRS


def test_exclude_globs_are_anchored_dir_globs() -> None:
    globs = exclude_globs()
    for name in AGENT_CONFIG_DIRS:
        assert f"/{name}/" in globs      # repo-root anchored
        assert f"**/{name}/" in globs    # nested
    # dir globs only (trailing slash) — never a bare/leading-dot wildcard
    assert all(g.endswith("/") for g in globs)
    assert ".*/" not in globs and "*" != "".join(globs)


@pytest.mark.parametrize(
    "path",
    [
        ".codex/session.json",
        "a/b/.claude/state.json",
        ".pi",
        "nested/deep/.junie/models/x.json",
        ".opencode/config.toml",
    ],
)
def test_matches_agent_config_paths(path: str) -> None:
    assert path_has_agent_config(path) is True


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",   # legit dot-dir
        ".gitignore",                 # legit dotfile
        ".env.example",               # legit dotfile
        "docs/claude-guide/intro.md", # substring 'claude' but not a segment
        "src/pistol.py",              # substring 'pi' but not a segment
        "app/codexample.txt",         # substring 'codex' but not a segment
        "README.md",
        "",
    ],
)
def test_does_not_match_legitimate_paths(path: str) -> None:
    assert path_has_agent_config(path) is False


def test_matching_is_segment_not_substring() -> None:
    # '.claudex' is a different segment and must NOT match '.claude'
    assert path_has_agent_config(".claudex/x") is False
    assert path_has_agent_config("x/.codex-backup/y") is False
