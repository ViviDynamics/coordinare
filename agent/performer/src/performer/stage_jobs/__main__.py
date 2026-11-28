"""423: ``python -m performer.stage_jobs <qare|progettare> <job dir>``, a stage job's entry.

The token arrives in :data:`~performer.stage_jobs.CREDENTIAL_ENV`. Taking it out of
``os.environ`` keeps it from the job's children, but the process's initial environment,
which ``/proc/<pid>/environ`` shows to anything in the container running as the same user,
would still hold it. So the entry starts itself again (``execve``) without it, and hands
the token over through a pipe that is read once and closed.

450: a QAre job then starts itself once more, after its checkout and diff and before QAre
runs anything of the pull request's: the process that waits on QAre, while that code runs
as root beside it, never held the token, so its memory has no copy either. What the fresh
process needs to go on (the checkout's directory, when the run started) arrives in
:data:`RESUME_ENV`; it holds no secret.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from performer.stage_jobs import CREDENTIAL_ENV

KINDS = ("qare", "progettare")
#: The arguments: the kind and the job directory.
_ARGS = 2
#: The descriptor the token is handed over on, after the entry started itself again.
HANDOVER_FD_ENV = "COORDINARE_STAGE_HANDOVER_FD"
#: 450: what a QAre job's last process goes on from, after the steps that need the token.
RESUME_ENV = "COORDINARE_STAGE_RESUME"


def _restart_without_token(args: list[str], token: str) -> None:
    """Replace this process with one whose initial environment holds no token."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, token.encode())
    os.close(write_fd)
    os.set_inheritable(read_fd, True)
    env = {k: v for k, v in os.environ.items() if k != CREDENTIAL_ENV}
    env[HANDOVER_FD_ENV] = str(read_fd)
    os.execve(sys.executable, [sys.executable, "-m", "performer.stage_jobs", *args], env)  # noqa: S606


def _restart_after_checkout(args: list[str], state: dict[str, Any]) -> None:
    """Replace this process, which held the token, with one that never did."""
    env = {k: v for k, v in os.environ.items() if k not in (CREDENTIAL_ENV, HANDOVER_FD_ENV)}
    env[RESUME_ENV] = json.dumps(state)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(sys.executable, [sys.executable, "-m", "performer.stage_jobs", *args], env)  # noqa: S606


def _handed_over_token() -> str:
    raw = os.environ.pop(HANDOVER_FD_ENV, "")
    if not raw.isdigit():
        return ""
    fd = int(raw)
    try:
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks).decode()
    except OSError:
        return ""
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    entry = argv is None
    args = list(sys.argv[1:] if argv is None else argv)
    # Out of the environment before anything else runs, so no child inherits it.
    token = os.environ.pop(CREDENTIAL_ENV, "")
    resume = os.environ.pop(RESUME_ENV, "")
    if token and entry and hasattr(os, "execve"):
        _restart_without_token(args, token)
    token = "" if resume else token or _handed_over_token()
    if len(args) != _ARGS or args[0] not in KINDS:
        sys.stderr.write(f"usage: python -m performer.stage_jobs <{'|'.join(KINDS)}> <job dir>\n")
        return 2
    kind, job_dir = args[0], Path(args[1])
    if resume and kind == "qare":
        from performer.stage_jobs import qare

        sys.stdout.write(f"stage job {kind} goes on without its token\n")
        sys.stdout.flush()
        qare.resume_job(job_dir, json.loads(resume))
        return 0
    # The job's log starts here: a job that ends with nothing logged never started.
    sys.stdout.write(f"stage job {kind} started\n")
    sys.stdout.flush()
    if kind == "qare":
        from performer.stage_jobs import qare

        if entry and hasattr(os, "execve"):
            qare.run_job(job_dir, token=token, restart=lambda state: _restart_after_checkout(args, state))
        else:
            qare.run_job(job_dir, token=token)
    else:
        from performer.stage_jobs import progettare

        progettare.run_job(job_dir, token=token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
