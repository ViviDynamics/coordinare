"""423: stage jobs, one stage provider's CLI run inside an ephemeral performer container.

Coordinare starts a container from the performer image with one job directory mounted at
the same path it has on coordinare's side, and the command::

    python -m performer.stage_jobs <qare|progettare> <job dir>

The job directory holds the request (``request.json``, no secret in it) and is where the
job reports how it ended. The checkout the CLI reads is made here, by the workspace layer
(:mod:`performer.stage_checkout`), in a directory of the container's own, never in the
mounted job directory: repository code never lands on coordinare's host (specs 107 and
555). The GitHub token arrives in :data:`CREDENTIAL_ENV` and is taken out of the
environment before anything else runs.

The file names and outcomes are a contract with coordinare's providers
(``coordinare.services.qare_runner`` and ``coordinare.services.progettare_runner``), which
read what is written here. The production daemon image does not ship this package, so
each side keeps its own copy and a test checks that they agree.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

#: The environment variable the job's GitHub token arrives in.
CREDENTIAL_ENV = "COORDINARE_STAGE_GITHUB_TOKEN"
#: Where a job's checkouts go: a directory of the container's own.
SCRATCH_PREFIX = "coordinare-stage-"


def write_atomic(path: Path, text: str) -> None:
    """A reader sees the old file or the new one, never half of one."""
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    write_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def scratch_dir() -> Path:
    """A fresh directory for the job's checkouts, outside the mounted job directory."""
    return Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))


def hand_back(job_dir: Path) -> None:
    """Give what the job wrote to the job directory's owner.

    The container runs as root; coordinare, which reads, publishes and later prunes these
    files, usually does not. Nothing changes when the job is not root.
    """
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    try:
        owner = job_dir.stat()
    except OSError:
        return
    for root, dirs, files in os.walk(job_dir):
        for name in (*dirs, *files):
            with contextlib.suppress(OSError):
                os.chown(Path(root) / name, owner.st_uid, owner.st_gid, follow_symlinks=False)
