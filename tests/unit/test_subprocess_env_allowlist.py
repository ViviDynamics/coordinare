"""051 / PR #219 — the host env a subprocess inherits, on both platforms.

The Windows half of this exists because ``_build_minimal_env`` strips the
environment to an allowlist, and on Windows that dropped ``SystemRoot``. Winsock
reads it to locate its DNS provider DLLs, so every lookup inside the subprocess
failed with "Could not resolve host" on a healthy network and git died before
opening a connection.

The tests are platform-independent on purpose: ``host_env_allowlist`` reads
``os.name`` when called, so the Windows branch is reachable from CI on Linux.
A fix verifiable only on the one host that has the bug is a fix nobody can
regression-test.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from coordinare.lib.subprocess_env import (
    BASE_ENV_ALLOWLIST,
    CREDENTIAL_BEARING_NAMES,
    WINDOWS_ENV_ALLOWLIST,
    host_env_allowlist,
    inherited_host_env,
)


class TestTheAllowlistIsPlatformAware:
    def test_posix_gets_the_base_list_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("coordinare.lib.subprocess_env.os.name", "posix")

        assert host_env_allowlist() == BASE_ENV_ALLOWLIST

    def test_windows_gets_the_windows_names_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("coordinare.lib.subprocess_env.os.name", "nt")

        allowlist = host_env_allowlist()

        assert set(BASE_ENV_ALLOWLIST) <= set(allowlist)
        assert set(WINDOWS_ENV_ALLOWLIST) <= set(allowlist)

    def test_systemroot_is_the_one_that_matters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Named alone because it is the difference between working and not.

        The others are process essentials git expects; without this one, DNS
        resolution inside the subprocess fails outright.
        """
        monkeypatch.setattr("coordinare.lib.subprocess_env.os.name", "nt")

        assert "SystemRoot" in host_env_allowlist()

    def test_the_base_list_leaks_nothing_new(self) -> None:
        """051's point is what is absent. A var added here is a deliberate act."""
        assert set(BASE_ENV_ALLOWLIST) == {
            "PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "LC_CTYPE",
        }

    def test_no_credential_shaped_name_is_allowlisted(self) -> None:
        """Neither list may carry a token, key or credential in its name."""
        banned = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL", "GITHUB")
        offenders = [
            name
            for name in BASE_ENV_ALLOWLIST + WINDOWS_ENV_ALLOWLIST
            if any(word in name.upper() for word in banned)
        ]

        assert not offenders, f"these would inherit host credentials: {offenders}"

    def test_no_name_that_merely_points_at_them_either(self) -> None:
        """The test above reads the spelling; this one reads the effect.

        Review's finding, and it was right: USERPROFILE, APPDATA and LOCALAPPDATA
        contain none of those words and are exactly where Windows keeps the
        user's ``.gitconfig`` and credential stores. Git reaches the host
        identity and ``credential.helper`` through USERPROFILE, which is the leak
        spec 051 exists to prevent -- so a name-shape check alone would have
        passed the very variables that reopen it.
        """
        offenders = sorted(
            CREDENTIAL_BEARING_NAMES.intersection(BASE_ENV_ALLOWLIST + WINDOWS_ENV_ALLOWLIST),
        )

        assert not offenders, (
            f"{offenders} point at where Windows keeps the user's git config and "
            "credentials. Measured on the runner: git resolves and clones without "
            "them, so there is nothing to trade away."
        )

    def test_it_copies_only_what_is_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("coordinare.lib.subprocess_env.os.name", "posix")
        monkeypatch.setenv("LANG", "en_GB.UTF-8")
        monkeypatch.delenv("LC_ALL", raising=False)

        env = inherited_host_env()

        assert env["LANG"] == "en_GB.UTF-8"
        assert "LC_ALL" not in env


class TestBothEnvBuildersUseTheOneList:
    """The defect PR #219 had: two copies of the allowlist, one of them fixed.

    ``subprocess_transport._build_subprocess_env`` held a literal copy, so the
    Windows names added to ``workspace``'s list left the performer's own
    environment -- the one that clones -- still unable to resolve a hostname.
    Structural, because the next person to change the list will change one site.
    """

    _SOURCES = (
        Path("src/coordinare/workspace.py"),
        Path("src/coordinare/transport/subprocess_transport.py"),
    )

    def test_neither_site_carries_its_own_copy(self) -> None:
        """Per function, not per tuple.

        Review pointed out that scanning individual tuples of four or more
        elements misses a list split across two smaller ones and concatenated --
        a plausible shape for exactly the drift this guards. So the strings are
        collected per enclosing function instead, which no amount of splitting
        or reordering inside that function escapes.
        """
        offenders = []
        for path in self._SOURCES:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                    continue
                scope = "module" if isinstance(node, ast.Module) else node.name
                # Module scope means the top level only, not everything nested in
                # it, or every function would be reported through its parent.
                body = node.body if isinstance(node, ast.Module) else [node]
                values = {
                    inner.value
                    for statement in body
                    for inner in ast.walk(statement)
                    if isinstance(inner, ast.Constant) and isinstance(inner.value, str)
                    if not (isinstance(node, ast.Module) and isinstance(statement, ast.FunctionDef))
                }
                if {"PATH", "HOME", "TMPDIR"} <= values:
                    offenders.append(f"{path.name}:{scope}")

        assert not offenders, (
            f"an env allowlist is spelled out at {offenders} instead of coming from "
            "coordinare.lib.subprocess_env. Two copies is how the Windows fix reached "
            "only one of them."
        )

    def test_both_reach_the_shared_helper(self) -> None:
        for path in self._SOURCES:
            source = path.read_text()

            assert "inherited_host_env" in source, (
                f"{path.name} builds a subprocess env without the shared allowlist"
            )

    def test_the_performer_env_carries_systemroot_on_windows(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The half the PR missed, asserted through the transport itself."""
        from coordinare.transport.subprocess_transport import SubprocessTransport

        monkeypatch.setattr("coordinare.lib.subprocess_env.os.name", "nt")
        monkeypatch.setenv("SystemRoot", r"C:\Windows")

        transport = SubprocessTransport("echo", 30, config=_Config())
        env = transport._build_subprocess_env()

        assert env.get("SystemRoot") == r"C:\Windows", (
            "the performer subprocess would fail every DNS lookup on Windows"
        )

    def test_the_git_env_carries_it_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coordinare.workspace import _build_minimal_env

        monkeypatch.setattr("coordinare.lib.subprocess_env.os.name", "nt")
        monkeypatch.setenv("SystemRoot", r"C:\Windows")

        assert _build_minimal_env(None).get("SystemRoot") == r"C:\Windows"

    def test_neither_env_carries_it_on_posix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A stray SystemRoot on a POSIX host stays out: the branch is guarded."""
        from coordinare.workspace import _build_minimal_env

        monkeypatch.setattr("coordinare.lib.subprocess_env.os.name", "posix")
        monkeypatch.setenv("SystemRoot", "/should-not-travel")

        assert "SystemRoot" not in _build_minimal_env(None)


class _Config:
    """Minimal stand-in: the transport only builds an isolated env with a config."""

    bot_identity = None
    env_passthrough = ()
