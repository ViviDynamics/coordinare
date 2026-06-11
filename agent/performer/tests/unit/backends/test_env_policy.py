"""Tests for the shared backend subprocess-env merge policy (spec 088 B1).

Every backend must launch its agent CLI with the same env shape:
image PATH first, env-cache toolchain dirs appended (deduplicated),
all non-PATH cache vars (LD_LIBRARY_PATH, RBENV_ROOT, ...) preserved,
and a system-default PATH fallback when the image PATH is empty.
"""

from __future__ import annotations

import os

from performer.backends._env_policy import SYSTEM_DEFAULT_PATH, build_subprocess_env


class TestPathPolicy:
    def test_cache_path_appended_after_image_path(self) -> None:
        env = build_subprocess_env(
            cache_env={"PATH": "/cache/.rbenv/shims:/cache/.nvm/node/bin"},
            base_env={"PATH": "/usr/local/bin:/usr/bin"},
        )
        assert env["PATH"] == (
            "/usr/local/bin:/usr/bin:/cache/.rbenv/shims:/cache/.nvm/node/bin"
        )

    def test_cache_path_dirs_already_on_image_path_are_deduplicated(self) -> None:
        env = build_subprocess_env(
            cache_env={"PATH": "/usr/bin:/cache/.rbenv/shims"},
            base_env={"PATH": "/usr/local/bin:/usr/bin"},
        )
        assert env["PATH"] == "/usr/local/bin:/usr/bin:/cache/.rbenv/shims"

    def test_empty_image_path_falls_back_to_system_default(self) -> None:
        # A cache-only PATH would put the project's pinned old node first and
        # crash a modern Node-based agent CLI at startup.
        env = build_subprocess_env(
            cache_env={"PATH": "/cache/.nvm/node/bin"},
            base_env={},
        )
        assert env["PATH"] == f"{SYSTEM_DEFAULT_PATH}:/cache/.nvm/node/bin"

    def test_no_cache_path_leaves_image_path_untouched(self) -> None:
        env = build_subprocess_env(
            cache_env={"RBENV_ROOT": "/cache/.rbenv"},
            base_env={"PATH": "/usr/bin"},
        )
        assert env["PATH"] == "/usr/bin"

    def test_cache_path_fully_contained_in_image_path_is_a_noop(self) -> None:
        env = build_subprocess_env(
            cache_env={"PATH": "/usr/bin"},
            base_env={"PATH": "/usr/local/bin:/usr/bin"},
        )
        assert env["PATH"] == "/usr/local/bin:/usr/bin"

    def test_empty_path_segments_in_cache_path_are_dropped(self) -> None:
        env = build_subprocess_env(
            cache_env={"PATH": ":/cache/bin::"},
            base_env={"PATH": "/usr/bin"},
        )
        assert env["PATH"] == "/usr/bin:/cache/bin"


class TestNonPathCacheVars:
    def test_non_path_cache_vars_are_preserved(self) -> None:
        env = build_subprocess_env(
            cache_env={
                "PATH": "/cache/bin",
                "LD_LIBRARY_PATH": "/cache/.devenv-libs/lib",
                "RBENV_ROOT": "/cache/.rbenv",
            },
            base_env={"PATH": "/usr/bin"},
        )
        assert env["LD_LIBRARY_PATH"] == "/cache/.devenv-libs/lib"
        assert env["RBENV_ROOT"] == "/cache/.rbenv"


class TestOverlayPrecedence:
    def test_git_tool_extra_overlay_in_order(self) -> None:
        env = build_subprocess_env(
            cache_env={"X": "cache"},
            git_env={"X": "git", "GIT_ONLY": "1"},
            tool_env={"X": "tool", "TOOL_ONLY": "1"},
            extra={"X": "extra", "EXTRA_ONLY": "1"},
            base_env={"X": "base"},
        )
        assert env["X"] == "extra"
        assert env["GIT_ONLY"] == "1"
        assert env["TOOL_ONLY"] == "1"
        assert env["EXTRA_ONLY"] == "1"

    def test_cache_env_overrides_base_env_for_non_path_vars(self) -> None:
        env = build_subprocess_env(
            cache_env={"LD_LIBRARY_PATH": "/cache/lib"},
            base_env={"LD_LIBRARY_PATH": "/image/lib"},
        )
        assert env["LD_LIBRARY_PATH"] == "/cache/lib"

    def test_base_env_vars_carried_through(self) -> None:
        env = build_subprocess_env(
            cache_env={},
            base_env={"HOME": "/root", "PATH": "/usr/bin"},
        )
        assert env["HOME"] == "/root"


class TestDefaults:
    def test_base_env_defaults_to_os_environ(self, monkeypatch) -> None:
        monkeypatch.setenv("ENV_POLICY_SENTINEL", "yes")
        monkeypatch.setenv("PATH", "/usr/bin")
        env = build_subprocess_env(cache_env={"PATH": "/cache/bin"})
        assert env["ENV_POLICY_SENTINEL"] == "yes"
        assert env["PATH"] == "/usr/bin:/cache/bin"

    def test_inputs_are_not_mutated(self) -> None:
        cache_env = {"PATH": "/cache/bin"}
        base_env = {"PATH": "/usr/bin"}
        build_subprocess_env(cache_env=cache_env, base_env=base_env)
        assert cache_env == {"PATH": "/cache/bin"}
        assert base_env == {"PATH": "/usr/bin"}

    def test_uses_os_pathsep(self) -> None:
        env = build_subprocess_env(
            cache_env={"PATH": os.pathsep.join(["/cache/a", "/cache/b"])},
            base_env={"PATH": "/usr/bin"},
        )
        assert env["PATH"].split(os.pathsep) == ["/usr/bin", "/cache/a", "/cache/b"]
