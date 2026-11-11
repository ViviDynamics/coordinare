"""Trusted repair-trial entrypoint: verify the pinned checkout, then run a trial manifest on a trial disk.

A trial is a repair's diagnostic run of replacement services and named checks on its failed generation's sealed trial
disk (repair-trial-amendment.md). Its report is an observation for the repair's author, never evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity
from coordinare_service_inference.environment.execution_source_checkout import checkout_identity
from coordinare_service_inference.environment.execution_source_install import MAX_CONTRACT_BYTES
from coordinare_service_inference.environment.models import Command, EnvironmentManifest, LocalCredential, Service
from coordinare_service_inference.environment.runner import run_trial
from coordinare_service_inference.environment.runtime import TrialReceipt
from coordinare_service_inference.environment.runtime_cli import run_cli
from coordinare_service_inference.environment.runtime_execution import trial_manifest
from coordinare_service_inference.environment.runtime_transport import consume_runtime_controls

TRIAL_KEYS = frozenset({"base", "request", "execution_source", "trial"})
MAX_TRIAL_SERVICES = 8
MAX_TRIAL_CHECKS = 16


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--scratch", required=True)
    args = parser.parse_args()
    raw = sys.stdin.buffer.read(2 * MAX_CONTRACT_BYTES + 1)
    if len(raw) > 2 * MAX_CONTRACT_BYTES:
        raise ValueError("trial contract exceeds byte bound")
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != TRIAL_KEYS:
        raise ValueError("trial contract invalid")
    base = EnvironmentManifest.model_validate(data["base"])
    request = data["request"]
    if (not isinstance(request, dict) or set(request) - {"services", "checks", "local_credentials"}
            or len(request.get("services") or []) > MAX_TRIAL_SERVICES or len(request.get("checks") or []) > MAX_TRIAL_CHECKS):
        raise ValueError("trial request invalid")
    # Each record is validated; the composed manifest is not, since a trial never carries the scope's whole mapping.
    manifest = trial_manifest(base, [Service.model_validate(item) for item in request.get("services") or []],
                              [Command.model_validate(item) for item in request.get("checks") or []],
                              [LocalCredential.model_validate(item) for item in request.get("local_credentials") or []])
    expected = ExecutionSourceIdentity.model_validate(data["execution_source"])
    trial = TrialReceipt.model_validate(data["trial"])
    runtime_deadline, _ = consume_runtime_controls(required=True, install=False, trial=True)
    if runtime_deadline is None:
        raise ValueError("trial requires a runtime deadline")

    async def run() -> object:
        actual = await checkout_identity(Path(args.repo), commit=base.source_commit, review_inputs=base.inputs)
        if actual != expected:
            raise ValueError("trial source differs from the failed generation's pinned tree")
        return await run_trial(base, manifest, trial=trial, cache=args.cache, repo=args.repo, scratch=args.scratch,
                               execution_source_digest=actual.digest(), runtime_deadline=runtime_deadline)

    report = run_cli(run())
    sys.stdout.write(report.model_dump_json() + "\n")  # type: ignore[attr-defined]
    raise SystemExit(0)


if __name__ == "__main__":
    main()
