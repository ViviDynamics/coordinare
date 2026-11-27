"""404/423: a QAre run's job directory, as coordinare's side of the stage job reads it.

A QAre run is a stage job (:mod:`coordinare.services.stage_jobs`): an ephemeral performer
container runs ``python -m performer.stage_jobs qare <job dir>``, makes the checkout with
the performer's workspace layer, and runs the pinned QAre there. Coordinare's host never
clones or fetches (specs 107 and 555, issue #423); nothing in this module starts a process.

The job directory is the run's whole state, shared with the container through a mount:

- ``request.json``: what to run, written by the provider (no secret in it);
- ``launch.json``: written by the provider once the container is started;
- ``status.json``: written last by the job, atomically, with one of the outcomes below;
- ``evidence/``, ``plan.json``, ``criteria.json``, ``change.diff``, ``runner.log``.

The names and outcomes are a contract with ``performer.stage_jobs.qare``; a test checks
that both sides agree.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

REQUEST_FILE = "request.json"
STATUS_FILE = "status.json"
LAUNCH_FILE = "launch.json"
LOG_FILE = "runner.log"
PLAN_FILE = "plan.json"
CRITERIA_FILE = "criteria.json"
DIFF_FILE = "change.diff"
EVIDENCE_DIR = "evidence"
#: The stage job kind (``python -m performer.stage_jobs qare``).
KIND = "qare"

#: Outcomes the job reports. The provider maps each onto the stage contract.
RAN = "ran"  # qare run ended; its exit code and result.json say how
UNAVAILABLE = "unavailable"  # qare could not be started at all
MISCONFIGURED = "misconfigured"  # the pinned version is not the one installed
NO_PROFILE = "no_profile"  # neither side of the change has a QA profile
NO_CRITERIA = "no_criteria"  # the work item states no acceptance criteria
ERROR = "error"  # a step before the verdict failed; nothing was decided
TIMEOUT = "timeout"  # the run took longer than timeout_seconds


def _write_atomic(path: Path, text: str) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)
