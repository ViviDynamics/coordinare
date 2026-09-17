"""The host variables a coordinare subprocess is allowed to inherit (051).

Spec 051 strips the environment before launching git or a performer, so neither
picks up the host user's credentials or git config. Two call sites needed that
list -- ``workspace._build_minimal_env`` and
``transport.subprocess_transport.SubprocessTransport._build_subprocess_env`` --
and each carried its own literal copy of it.

They are here instead, because the copies drifted the moment the list had to
change: a Windows fix applied to one of them left the other stripping
``SystemRoot``, which is the variable Winsock reads to find its DNS provider
DLLs. A performer that cannot resolve a hostname cannot clone, so the half that
was missed is the half that does the network work.
"""

from __future__ import annotations

import os

#: Enough for git to find its binaries, a home directory, a temp dir and a
#: locale. Deliberately short: everything absent here is something a subprocess
#: cannot see, which is the point of the feature.
BASE_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
)

#: Windows resolves DNS through Winsock, which reads ``SystemRoot`` to locate its
#: provider DLLs. A subprocess launched without it cannot resolve a hostname on a
#: perfectly healthy network, so git dies before it opens a connection.
#:
#: Measured rather than assumed, on the org's Windows runner
#: (.github/workflows/windows-git-env.yml). Removing ``SystemRoot`` and ``windir``
#: from this list and running ``git ls-remote`` gives::
#:
#:     fatal: unable to access 'https://github.com/git/git.git/':
#:     getaddrinfo() thread failed to start
#:
#: which is worth writing down, because it is not the "Could not resolve host"
#: this was first reported as -- anyone grepping for that message would conclude
#: the cause was something else. With the list intact the same call exits 0.
#:
#: The rest are the process essentials git needs to locate binaries and spawn
#: helpers.
#:
#: ``USERPROFILE``, ``APPDATA`` and ``LOCALAPPDATA`` were here and are gone.
#: Windows git finds the user's ``.gitconfig`` through ``USERPROFILE``, and that
#: file carries ``user.name``, ``user.email`` and ``credential.helper`` -- which
#: is the leak spec 051 was written for, in its own words: "the performer was
#: using the host user's personal GITHUB_TOKEN and git identity". Allowlisting
#: them would have reopened on Windows the hole 051 closed on POSIX.
#:
#: Whether git needs them is a fact about Windows, so it was measured rather than
#: argued: on the runner, ``git ls-remote`` over HTTPS exits 0 without all three
#: (``--credential-boundary`` in the probe). Coordinare does not want the host's
#: git config anyway -- it sets identity through ``GIT_AUTHOR_*``/``GIT_COMMITTER_*``
#: and authenticates through ``GIT_CONFIG_KEY_*`` http.extraHeader.
WINDOWS_ENV_ALLOWLIST: tuple[str, ...] = (
    "SystemRoot",
    "SystemDrive",
    "windir",
    "ComSpec",
    "PATHEXT",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramData",
)

#: Names that must never be allowlisted: each is a path to where Windows keeps
#: the user's git config or credentials, so none of them is credential-shaped by
#: name while all of them are credential-bearing in effect. Named here so the
#: test can assert on the reasoning rather than on the spelling.
CREDENTIAL_BEARING_NAMES: frozenset[str] = frozenset(
    {"USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "USERNAME"},
)


def host_env_allowlist() -> tuple[str, ...]:
    """The variable names a subprocess may inherit on this platform.

    Read ``os.name`` at call time rather than at import, so a test can exercise
    the Windows branch on any host -- and so the answer cannot be baked in by
    whichever platform happened to import the module first.
    """
    if os.name == "nt":
        return BASE_ENV_ALLOWLIST + WINDOWS_ENV_ALLOWLIST
    return BASE_ENV_ALLOWLIST


def inherited_host_env() -> dict[str, str]:
    """The allowlisted host variables that are actually set, copied out."""
    return {key: os.environ[key] for key in host_env_allowlist() if key in os.environ}
