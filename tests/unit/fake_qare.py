"""404: a stand-in for the QAre CLI, following its orchestrator contract.

Run as ``python fake_qare.py <args>``. What it does is read from the JSON file named by
``FAKE_QARE_SCENARIO`` (which reaches it only through ``env_passthrough``):

- ``version``: what ``--version`` prints (default ``VERSION``);
- ``plan_exit``: the exit code of ``plan`` (default 0);
- ``verdict``: the verdict ``run`` writes (default ``passed``), and ``exit`` its exit code
  (default the contract's code for the verdict);
- ``outcomes``: criterion id -> ``proven`` / ``failed`` / ``unverified`` (default proven);
- ``schema``: the ``schemaVersion`` written (default ``"1"``); ``raw``: written verbatim
  instead of a result.

Every call is appended to ``<scenario>.calls.jsonl`` with its arguments and the names of
the environment variables it could see.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

VERSION = "2026.11.42"
EXIT = {"passed": 0, "failed": 1, "blocked": 2, "refused": 3, "waived": 5}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def main(argv: list[str]) -> int:
    scenario_path = os.environ.get("FAKE_QARE_SCENARIO", "")
    scenario = json.loads(Path(scenario_path).read_text()) if scenario_path else {}
    if scenario_path:
        with Path(f"{scenario_path}.calls.jsonl").open("a") as calls:
            calls.write(json.dumps({"argv": argv, "env": sorted(os.environ), "cwd": os.getcwd()}) + "\n")
    if argv[:1] == ["--version"]:
        sys.stdout.write(f"{scenario.get('version', VERSION)}\n")
        return 0
    if argv[:1] == ["plan"]:
        code = int(scenario.get("plan_exit", 0))
        if code:
            sys.stderr.write("plan: the planner could not run\n")
            return code
        criteria = json.loads(Path(_flag(argv, "--criteria")).read_text())
        plan = {"schemaVersion": "1", "criteria": [
            {"id": c["id"], "text": c["text"], "checks": [{"kind": "command", "name": "t", "command": "true"}]}
            for c in criteria
        ]}
        Path(_flag(argv, "--out")).write_text(json.dumps(plan))
        return 0
    if argv[:1] == ["run"]:
        evidence = Path(_flag(argv, "--evidence"))
        evidence.mkdir(parents=True, exist_ok=True)
        if "raw" in scenario:
            (evidence / "result.json").write_text(scenario["raw"])
            return int(scenario.get("exit", 4))
        plan = json.loads(Path(_flag(argv, "--plan")).read_text())
        verdict = scenario.get("verdict", "passed")
        outcomes = scenario.get("outcomes", {})
        criteria = []
        for c in plan["criteria"]:
            outcome = outcomes.get(c["id"], "proven")
            checks = evidence / "checks" / c["id"] / "1"
            checks.mkdir(parents=True, exist_ok=True)
            (checks / "stdout.txt").write_text(f"checked {c['id']}\n")
            (checks / "screen.png").write_bytes(PNG)
            entry = {"id": c["id"], "outcome": outcome,
                     "evidence": [f"checks/{c['id']}/1/stdout.txt", f"checks/{c['id']}/1/screen.png"]}
            if outcome == "unverified":
                entry["reason"] = "the stub for payments is missing"
            criteria.append(entry)
        result = {"schemaVersion": scenario.get("schema", "1"), "verdict": verdict,
                  "job": {"id": _flag(argv, "--id")}, "criteria": criteria}
        if verdict == "waived":
            result["waived"] = [{"criterionId": criteria[0]["id"], "by": "alice"}]
        (evidence / "result.json").write_text(json.dumps(result))
        return int(scenario.get("exit", EXIT.get(verdict, 4)))
    sys.stderr.write(f"fake qare: unknown command {argv}\n")
    return 4


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
