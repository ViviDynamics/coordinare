"""Does a stripped Windows environment actually break git's DNS? (PR #219)

Run by .github/workflows/windows-git-env.yml on the org's self-hosted Windows
runner. The point is to test the claim rather than the comment: coordinare strips
the subprocess environment to an allowlist (spec 051), and the assertion is that
omitting ``SystemRoot`` makes every DNS lookup inside that subprocess fail,
because Winsock reads it to find its provider DLLs.

Two modes, ordered so the first establishes that the failure is real before the
second claims to fix it:

--without-systemroot   git ls-remote under the allowlisted env with SystemRoot
                       removed. Expected to FAIL to resolve. If it succeeds, the
                       premise of the fix does not hold on this host and the
                       script says so rather than passing.
--with-coordinare-env   the same call under the env coordinare builds. Expected to
                       succeed.

``coordinare.lib.subprocess_env`` is loaded straight from its file rather than
imported as part of the package: ``coordinare/__init__`` pulls in structlog, and
nothing installs coordinare's dependencies on this runner. Installing them would
mutate a shared box to test a module whose only import is ``os``. That the two
env builders *use* this list is asserted by
tests/unit/test_subprocess_env_allowlist.py, on every platform, which is where a
structural claim belongs anyway.

Exit 0 means the observed behaviour matched what the fix assumes.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

# A host that must resolve for the probe to say anything about DNS. Not the repo's
# own remote: that would conflate resolution with auth and rate limits.
PROBE_URL = "https://github.com/git/git.git"

RESOLUTION_MARKERS = (
    "could not resolve host",
    "could not resolve proxy",
    "name or service not known",
    "getaddrinfo",
)

_MODULE_PATH = Path(__file__).resolve().parents[2] / "src" / "coordinare" / "lib" / "subprocess_env.py"


def _load_subprocess_env():
    """The real module, by path, with no package import and no dependencies."""
    spec = importlib.util.spec_from_file_location("_probe_subprocess_env", _MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ls_remote(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "ls-remote", "--heads", PROBE_URL, "HEAD"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _report(label: str, result: subprocess.CompletedProcess[str]) -> str:
    combined = (result.stdout + result.stderr).strip()
    print(f"--- {label}: exit={result.returncode}")
    for line in combined.splitlines()[:12]:
        print(f"    {line}")
    return combined.lower()


def without_systemroot(module) -> int:
    env = module.inherited_host_env()
    env["GIT_TERMINAL_PROMPT"] = "0"
    removed = {key: env.pop(key) for key in ("SystemRoot", "windir") if key in env}
    print(f"env vars: {sorted(env)}")
    print(f"removed:  {sorted(removed)}")
    if not removed:
        print("FAILED: SystemRoot was not in the allowlisted env to begin with.")
        print("Either this is not Windows, or the allowlist no longer includes it.")
        return 1

    lowered = _report("stripped env", _ls_remote(env))
    if any(marker in lowered for marker in RESOLUTION_MARKERS):
        print("EXPECTED: resolution failed without SystemRoot. The premise holds.")
        return 0

    # Not something to argue away: if this host resolves fine without SystemRoot,
    # the Windows allowlist is not buying what it claims to, and that is worth
    # knowing before it ships.
    print("UNEXPECTED: resolution worked without SystemRoot on this host.")
    print("The fix's premise does not hold here. Do not merge on this evidence.")
    return 2


def with_coordinare_env(module) -> int:
    env = module.inherited_host_env()
    env["GIT_TERMINAL_PROMPT"] = "0"
    print(f"env vars: {sorted(env)}")
    print(f"SystemRoot present: {'SystemRoot' in env}")

    lowered = _report("coordinare env", _ls_remote(env))
    if any(marker in lowered for marker in RESOLUTION_MARKERS):
        print("FAILED: coordinare's own allowlisted env still cannot resolve a host.")
        return 1
    print("OK: git resolved and connected under the env coordinare builds.")
    return 0


#: The user-directory variables. Review asked whether allowlisting these reopens
#: the hole spec 051 exists to close: on Windows git finds the host user's
#: .gitconfig through USERPROFILE, and that file carries user.name, user.email
#: and credential.helper -- which is precisely the leak 051 was written for
#: ("the performer was using the host user's personal GITHUB_TOKEN and git
#: identity"). The question of whether git still works without them is a fact
#: about Windows, so it is measured here rather than argued.
USER_DIR_VARS = ("USERPROFILE", "APPDATA", "LOCALAPPDATA")


def _git_config_origins(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "config", "--list", "--show-origin"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def credential_boundary(module) -> int:
    """Are the user-directory vars needed, and what do they expose?"""
    full = module.inherited_host_env()
    full["GIT_TERMINAL_PROMPT"] = "0"
    reduced = {k: v for k, v in full.items() if k not in USER_DIR_VARS}

    print(f"with user dirs:    {sorted(full)}")
    print(f"without user dirs: {sorted(reduced)}")

    # What the host config exposes when the user dirs are visible. Not a pass/fail
    # on its own -- a runner whose user has no .gitconfig would show nothing while
    # a developer's machine shows plenty -- but it is the evidence for the claim.
    for label, env in (("with user dirs", full), ("without user dirs", reduced)):
        result = _git_config_origins(env)
        files = sorted(
            {
                line.split("\t", 1)[0].removeprefix("file:")
                for line in result.stdout.splitlines()
                if line.startswith("file:")
            }
        )
        identity = [
            line for line in result.stdout.splitlines() if "user.name" in line or "user.email" in line
        ]
        helpers = [line for line in result.stdout.splitlines() if "credential.helper" in line]
        print(f"--- git config, {label}")
        print(f"    config files read: {files or 'none'}")
        print(f"    host identity visible: {identity or 'none'}")
        print(f"    credential helpers visible: {helpers or 'none'}")

    lowered = _report("ls-remote without user dirs", _ls_remote(reduced))
    if any(marker in lowered for marker in RESOLUTION_MARKERS):
        print("NEEDED: git cannot resolve without the user-directory vars.")
        print("Keeping them is then a trade, not an oversight. Say so in the list.")
        return 1
    print("NOT NEEDED: git resolved and connected without USERPROFILE/APPDATA/LOCALAPPDATA.")
    print("They can come off the allowlist, which is what 051 asks for.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--without-systemroot", action="store_true")
    group.add_argument("--with-coordinare-env", action="store_true")
    group.add_argument("--credential-boundary", action="store_true")
    args = parser.parse_args()

    print(f"os.name={os.name} platform={sys.platform} module={_MODULE_PATH}")
    if os.name != "nt":
        print("This probe only means anything on Windows. Refusing to pretend otherwise.")
        return 1

    module = _load_subprocess_env()
    print(f"allowlist: {list(module.host_env_allowlist())}")
    if args.without_systemroot:
        return without_systemroot(module)
    if args.credential_boundary:
        return credential_boundary(module)
    return with_coordinare_env(module)


if __name__ == "__main__":
    raise SystemExit(main())
