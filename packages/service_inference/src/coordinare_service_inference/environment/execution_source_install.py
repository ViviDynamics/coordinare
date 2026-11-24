"""Trusted installer entrypoint: verify the pinned checkout before project actions."""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity
from coordinare_service_inference.environment.execution_source_checkout import checkout_identity
from coordinare_service_inference.environment.models import EnvironmentManifest
from coordinare_service_inference.environment.runner import run_manifest
from coordinare_service_inference.environment.runtime_cli import run_cli
from coordinare_service_inference.environment.runtime_execution import PrivateRuntimeIncompleteError
from coordinare_service_inference.environment.runtime_transport import consume_runtime_controls
from coordinare_service_inference.environment.scope import SCOPE_CONTRACT_MAX_BYTES

if TYPE_CHECKING:
    from coordinare_service_inference.environment.models import VerificationReport

# The accepted manifest embeds its scope contract, which may reach SCOPE_CONTRACT_MAX_BYTES; the rest (inputs, commands,
# requirements) gets the same again. Dettmore e2e-19's 608 KB manifest, a 518 KB contract after several reviewed reuses,
# crashed the installer against the old fixed 512 KiB bound before any command ran.
MAX_CONTRACT_BYTES = 2 * SCOPE_CONTRACT_MAX_BYTES


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    raw = sys.stdin.buffer.read(MAX_CONTRACT_BYTES + 1)
    if len(raw) > MAX_CONTRACT_BYTES:
        raise ValueError("installer source contract exceeds byte bound")
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != {"manifest", "execution_source"}:
        raise ValueError("installer source contract invalid")
    manifest = EnvironmentManifest.model_validate(data["manifest"])
    expected = ExecutionSourceIdentity.model_validate(data["execution_source"])
    runtime_deadline, runtime_artifacts = consume_runtime_controls(required=manifest.runtime is not None, install=True)

    async def install() -> VerificationReport:
        actual = await checkout_identity(
            Path(args.repo), commit=manifest.source_commit, review_inputs=manifest.inputs,
        )
        if actual != expected:
            raise ValueError("installer source differs from accepted pinned tree")
        # nare-native-033: the installer's --repo is a bind mount, which cannot be moved, so an editable install linked
        # back to it resolved in every installer check and failed only in the clean consumer. The verified checkout is
        # copied into a directory this installer owns, and the installs, the guest's {repo} and the checks all use the
        # copy, which the runner moves out of reach once the installs are done.
        repo, scratch = Path(args.repo).resolve(), Path(args.scratch).resolve()
        owned = (contextlib.nullcontext(None) if scratch == repo or repo in scratch.parents
                 else tempfile.TemporaryDirectory(prefix="coordinare-install-source-", dir=scratch))
        with owned as directory:
            source = repo if directory is None else Path(directory) / "repo"
            if directory is not None:
                shutil.copytree(repo, source, symlinks=True)
            try:
                return await run_manifest(
                    manifest,
                    repo=str(source),
                    cache=args.cache,
                    scratch=args.scratch,
                    install=True,
                    execution_source_digest=actual.digest(),
                    runtime_deadline=runtime_deadline, runtime_artifacts=runtime_artifacts,
                )
            except PrivateRuntimeIncompleteError as exc:
                # The run holds either way; emit the evidence instead of only a traceback.
                if exc.report is None:
                    raise
                return exc.report

    report = run_cli(install())
    sys.stdout.write(report.model_dump_json() + "\n")
    raise SystemExit(0 if report.passed and not report.runtime_incomplete else 1)


if __name__ == "__main__":
    main()
