"""423: ``python -m performer.stage_jobs <qare|progettare> <job dir>``, a stage job's entry."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from performer.stage_jobs import CREDENTIAL_ENV

KINDS = ("qare", "progettare")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Out of the environment before anything else runs, so no child inherits it.
    token = os.environ.pop(CREDENTIAL_ENV, "")
    if len(args) != 2 or args[0] not in KINDS:  # noqa: PLR2004 - kind and job dir
        sys.stderr.write(f"usage: python -m performer.stage_jobs <{'|'.join(KINDS)}> <job dir>\n")
        return 2
    kind, job_dir = args[0], Path(args[1])
    if kind == "qare":
        from performer.stage_jobs import qare

        qare.run_job(job_dir, token=token)
    else:
        from performer.stage_jobs import progettare

        progettare.run_job(job_dir, token=token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
