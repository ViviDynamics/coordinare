"""403: a stand-in for the released ``progettare`` CLI, for tests.

Speaks the CLI's surface (``--version``, ``blueprint --issue --repo --config --runs-dir``)
and writes a run directory with Progettare's artifact shapes (contract version 1). The
``FAKE_PROGETTARE_*`` environment variables choose what the run does; the runner only
passes them through when the provider's ``pass_env`` names them, as for any variable.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

VERSION = "2026.11.3"


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    version = os.environ.get("FAKE_PROGETTARE_VERSION", VERSION)
    if argv == ["--version"]:
        print(version)
        return 0
    if not argv or argv[0] != "blueprint":
        print("usage: progettare blueprint ...", file=sys.stderr)
        return 1
    args = dict(zip(argv[1::2], argv[2::2], strict=False))
    runs = Path(args["--runs-dir"])
    invocation = {
        "argv": argv,
        "gh_token": os.environ.get("GH_TOKEN", ""),
        "env": sorted(os.environ),
        "checkout": sorted(os.listdir(args["--repo"])),
        "config_exists": Path(args["--config"]).is_file(),
    }
    with (runs.parent / "fake-invocations.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(invocation) + "\n")
    mode = os.environ.get("FAKE_PROGETTARE_MODE", "complete")
    if mode == "sleep":
        time.sleep(float(os.environ.get("FAKE_PROGETTARE_SLEEP", "30")))
    if mode == "crash":
        print("progettare failed: something broke before the run", file=sys.stderr)
        return 1
    stamp: dict[str, Any] = {
        "artifact_version": int(os.environ.get("FAKE_PROGETTARE_ARTIFACT_VERSION", "1")),
        "progettare": version,
        "written_at": "20261127T000000Z",
    }
    run = runs / "20261127T000000Z-acme-widgets-12"
    run.mkdir(parents=True)
    blocked = mode == "blocked"
    questions = ["What are the acceptance criteria for this issue?"] if blocked else []
    _write(run / "intake.json", {
        **stamp, "artifact": "intake", "status": "blocked" if blocked else "ok",
        "issue": {
            "owner": "acme", "repo": "widgets", "number": 12,
            "title": os.environ.get("FAKE_PROGETTARE_TITLE", ""),
            "body": os.environ.get("FAKE_PROGETTARE_BODY", ""),
            "state": "OPEN", "url": "https://github.com/acme/widgets/issues/12", "comments": [],
        },
        "acceptance_criteria": [], "clarifications": [], "conversation": [],
        "blocked_questions": questions,
    })
    manifest = {**stamp, "artifact": "run", "version": 2, "config_digest": "0" * 64, "stages": {}}
    if blocked:
        _write(run / "run.json", {**manifest, "status": "blocked", "failing_stage": "intake",
                                  "failing_reason": "; ".join(questions)})
        return 2
    if mode == "failed":
        _write(run / "run.json", {**manifest, "status": "failed", "failing_stage": "survey",
                                  "failing_reason": "nare exited 1"})
        return 1
    _write(run / "size.json", {**stamp, "artifact": "size", "version": 1, "config_version": 1,
                               "classification": "single_turn", "documenter": True})
    (run / "briefs").mkdir()
    brief = {"version": 1, "config_version": 1}
    _write(run / "briefs" / "implementer.json", {**stamp, **brief, "artifact": "implementer_brief", "brief": {
        "milestones": [{"title": "CSV export", "changes": ["add export.py", "add a download route"]}],
    }})
    _write(run / "briefs" / "qa.json", {**stamp, **brief, "artifact": "qa_brief", "brief": {
        "testable_criteria": ["GET /widgets.csv returns every widget"],
    }})
    if os.environ.get("FAKE_PROGETTARE_DOCUMENTER", "1") == "1":
        _write(run / "briefs" / "documenter.json", {**stamp, **brief, "artifact": "documenter_brief", "brief": {
            "topics": ["the CSV export in the user guide"],
        }})
    _write(run / "run.json", {**manifest, "status": "complete", "failing_stage": None, "failing_reason": None})
    print(json.dumps({"status": "complete", "run_dir": str(run)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
