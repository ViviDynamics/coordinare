"""Trusted installer entrypoint: verify the pinned checkout before project actions."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity
from coordinare_service_inference.environment.execution_source_checkout import checkout_identity
from coordinare_service_inference.environment.models import EnvironmentManifest
from coordinare_service_inference.environment.runner import run_manifest
from coordinare_service_inference.environment.runtime_transport import consume_runtime_controls


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    raw = sys.stdin.buffer.read(512 * 1024 + 1)
    if len(raw) > 512 * 1024:
        raise ValueError("installer source contract exceeds byte bound")
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {"manifest", "execution_source"}:
        raise ValueError("installer source contract invalid")
    manifest = EnvironmentManifest.model_validate(data["manifest"])
    expected = ExecutionSourceIdentity.model_validate(data["execution_source"])
    runtime_deadline, runtime_artifacts = consume_runtime_controls(required=manifest.runtime is not None, install=True)

    async def install() -> int:
        actual = await checkout_identity(
            Path(args.repo), commit=manifest.source_commit, review_inputs=manifest.inputs,
        )
        if actual != expected:
            raise ValueError("installer source differs from accepted pinned tree")
        report = await run_manifest(
            manifest,
            repo=args.repo,
            cache=args.cache,
            scratch=args.scratch,
            install=True,
            execution_source_digest=actual.digest(),
            runtime_deadline=runtime_deadline, runtime_artifacts=runtime_artifacts,
        )
        sys.stdout.write(report.model_dump_json() + "\n")
        return 0 if report.passed else 1

    raise SystemExit(asyncio.run(install()))


if __name__ == "__main__":
    main()
