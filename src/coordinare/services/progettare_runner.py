"""403/423: a Progettare request's directory, as coordinare's side of the stage job reads it.

A Progettare run is a stage job (:mod:`coordinare.services.stage_jobs`): an ephemeral
performer container runs ``python -m performer.stage_jobs progettare <request dir>``,
clones the default branch with the performer's workspace layer, runs ``progettare
blueprint`` there, and drops the clone. Coordinare's host never clones (specs 107 and 555,
issue #423); nothing in this module starts a process.

The request directory under ``runs_dir`` (mounted into the container) holds:

- ``request.json``: what to plan, written by the provider (no secret in it);
- ``progettare-config.yaml``: the operator's ``config_path``, copied in when one is set;
- ``runs/``: Progettare's run directory, with its artifacts and ``run.json``;
- ``runner.json``: written by the job when it ends, saying in which phase and how;
- ``stdout.txt``, ``stderr.txt``, ``runner.log``.

The names are a contract with ``performer.stage_jobs.progettare``; a test checks that both
sides agree.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

REQUEST_FILE = "request.json"
RESULT_FILE = "runner.json"
RUNS_DIR = "runs"
LOG_FILE = "runner.log"
#: Where the operator's ``config_path`` is copied, so the job reads it from its mount.
CONFIG_FILE = "progettare-config.yaml"
#: The stage job kind (``python -m performer.stage_jobs progettare``).
KIND = "progettare"
#: Marks a request whose start failed while its container could not be confirmed gone.
UNCONFIRMED_FILE = "start-unconfirmed"
#: Files of an earlier attempt in the same directory, removed before a run starts.
STALE_FILES = (RESULT_FILE, "stdout.txt", "stderr.txt", LOG_FILE, CONFIG_FILE, UNCONFIRMED_FILE)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically: a reader sees the old file or the new one, never half."""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    Path(handle.name).replace(path)
