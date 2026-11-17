"""Isolation for the carrier containers that run a manifest's commands: probes, the installer, repair trials and the
clean consumer all use the same identity and syscall filter, so a probe observes what an install or check will get.

Every command runs as an unprivileged user with no Linux capabilities, as on a developer machine or a CI runner. The
syscall filter is Docker's own default profile plus one rule: the namespace and mount calls an unprivileged sandbox
tool such as bubblewrap needs. Without that rule Docker allows those calls only with CAP_SYS_ADMIN, which the carrier
never has. The kernel still requires capabilities inside the new user namespace, so the calls grant nothing outside it.
/proc is unmasked because a fresh procfs cannot be mounted over Docker's masked paths (nare-native-008: bubblewrap's
``--proc /proc`` failed with EPERM even once the namespace was allowed). As a non-root user without capabilities the
carrier still cannot write the kernel settings that unmasking exposes.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import tempfile
from functools import cache
from pathlib import Path
from typing import Any

# The coordinare daemon's own unprivileged uid (deploy/helm runAsUser), so a daemon-created directory on a Linux host
# is already owned by the carrier. Each carrier image defines this user, because ssh and ssh-keygen (the private
# runtime's guest control) refuse to run for a uid with no passwd entry.
CARRIER_UID = 10001
CARRIER_USER = f'{CARRIER_UID}:{CARRIER_UID}'
CARRIER_ACCOUNT = 'coordinare'
# Under /tmp, every carrier's writable scratch; tools such as uv, pip and git keep their per-user state under HOME and
# create it on first use. Never /tmp itself: the private runtime refuses to export the user's home as its scratch.
CARRIER_HOME = '/tmp/home'  # noqa: S108, RUF100 - a carrier container path, never opened on this host

# Docker's default profile, verbatim from moby/moby at tag docker-v29.9.3
# (vendor/github.com/moby/profiles/seccomp/default.json, Apache License 2.0; see NOTICE).
DOCKER_DEFAULT_PROFILE = Path(__file__).with_name('docker_default_seccomp.json')
DOCKER_DEFAULT_PROFILE_SHA256 = '785b2429264afba4d594320337cb17f144f3c7d51585f9805eef72e28f4f9334'
USER_NAMESPACE_SYSCALLS = ('clone', 'mount', 'pivot_root', 'umount2', 'unshare')
USER_NAMESPACE_RULE: dict[str, Any] = {
    'names': list(USER_NAMESPACE_SYSCALLS), 'action': 'SCMP_ACT_ALLOW',
    'comment': 'coordinare: unprivileged user namespaces for sandbox tools such as bubblewrap; the kernel still '
               'requires capabilities inside the new namespace'}

CARRIER_IMAGE_USER_STEP = (
    f'getent passwd {CARRIER_UID} >/dev/null || {{ groupadd -g {CARRIER_UID} {CARRIER_ACCOUNT} && '
    f'useradd -u {CARRIER_UID} -g {CARRIER_UID} -M -d {CARRIER_HOME} -s /bin/bash {CARRIER_ACCOUNT}; }}')


def carrier_seccomp_profile() -> dict[str, Any]:
    """Docker's default profile with the user-namespace rule appended; any other change to the vendored file refuses."""
    raw = DOCKER_DEFAULT_PROFILE.read_bytes()
    if hashlib.sha256(raw).hexdigest() != DOCKER_DEFAULT_PROFILE_SHA256:
        message = 'vendored Docker seccomp profile differs from its recorded digest'
        raise RuntimeError(message)
    profile: dict[str, Any] = json.loads(raw)
    profile['syscalls'] = [*profile['syscalls'], dict(USER_NAMESPACE_RULE)]
    return profile


@cache
def carrier_seccomp_path() -> str:
    """The derived profile as a file, for docker's --security-opt seccomp=<path>. It lives in a directory private to
    this process, so no other local user can replace it between writing and docker reading it."""
    content = json.dumps(carrier_seccomp_profile(), sort_keys=True, separators=(',', ':')).encode()
    directory = Path(tempfile.mkdtemp(prefix='coordinare-carrier-seccomp-'))
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    path = directory / 'profile.json'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(content)
    return str(path)


def carrier_isolation_options() -> list[str]:
    """docker run options shared by every carrier that runs manifest or probe commands."""
    return ['--user', CARRIER_USER, '-e', f'HOME={CARRIER_HOME}', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--security-opt', f'seccomp={carrier_seccomp_path()}',
            '--security-opt', 'systempaths=unconfined']
