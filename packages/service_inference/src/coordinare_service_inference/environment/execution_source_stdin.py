"""Trusted binary stdin bootstrap for an isolated clean verifier."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from coordinare_service_inference.environment.execution_source import (
    CHUNK_BYTES,
    MAX_ARCHIVE_BYTES,
    ExecutionSourceIdentity,
)
from coordinare_service_inference.environment.execution_source_restore import (
    restore_execution_bundle,
)


def run() -> int:
    header = sys.stdin.buffer.readline(1024 * 1024 + 1)
    if len(header) > 1024 * 1024 or not header.endswith(b"\n"):
        raise ValueError("execution source frame header invalid")
    data = json.loads(header)
    required = {
        "schema_version",
        "payload_bytes",
        "payload_sha256",
        "script_bytes",
        "script_sha256",
        "identity",
        "review_inputs",
    }
    if not isinstance(data, dict) or set(data) != required or data["schema_version"] != 1:
        raise ValueError("execution source frame fields invalid")
    for name, ceiling in [("payload_bytes", MAX_ARCHIVE_BYTES), ("script_bytes", 1024 * 1024)]:
        if type(data[name]) is not int or not 0 <= data[name] <= ceiling:
            raise ValueError("execution source frame length invalid")
    identity = ExecutionSourceIdentity.model_validate(data["identity"])
    if not isinstance(data["review_inputs"], dict):
        raise ValueError("execution source review hashes invalid")
    with tempfile.TemporaryDirectory(prefix="coordinare-source-") as temporary:
        parent = Path(temporary)
        bundle = parent / "source.tgz"
        digest = hashlib.sha256()
        remaining = data["payload_bytes"]
        with bundle.open("xb") as output:
            while remaining:
                chunk = sys.stdin.buffer.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise ValueError("execution source frame truncated")
                remaining -= len(chunk)
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != data["payload_sha256"]:
            raise ValueError("execution source transport digest differs")
        script = sys.stdin.buffer.read(data["script_bytes"])
        if (
            len(script) != data["script_bytes"]
            or hashlib.sha256(script).hexdigest() != data["script_sha256"]
            or sys.stdin.buffer.read(1)
        ):
            raise ValueError("execution source script frame differs")
        root = parent / "repo"
        restore_execution_bundle(
            bundle, root, expected=identity, review_inputs=data["review_inputs"],
        )
        env = dict(
            os.environ,
            COORDINARE_REPO_DIR=str(root),
            COORDINARE_ENV_EXECUTION_SOURCE_DIGEST=identity.digest(),
        )
        return subprocess.run(["/bin/bash", "-s"], input=script, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(run())
