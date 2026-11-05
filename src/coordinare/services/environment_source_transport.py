"""Bounded binary source framing for isolated execution, never exec/env payloads."""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, Any

from coordinare_service_inference.environment.execution_source import (
    CHUNK_BYTES,
    MAX_ARCHIVE_BYTES,
    canonical,
    validate_execution_bundle,
)
from coordinare_service_inference.environment.execution_source import (
    source_refusal as _refuse,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from coordinare_service_inference.environment.execution_source import ExecutionSourceBundle

EXECUTION_CHUNKS_PER_SECRET = 8
MAX_EXECUTION_SOURCE_SECRETS = 1 + (
    MAX_ARCHIVE_BYTES + CHUNK_BYTES * EXECUTION_CHUNKS_PER_SECRET - 1
) // (CHUNK_BYTES * EXECUTION_CHUNKS_PER_SECRET)
MAX_HEADER_BYTES = 1024 * 1024
MAX_SCRIPT_BYTES = 1024 * 1024


def execution_header(
    bundle: ExecutionSourceBundle,
    script: str,
    *,
    review_inputs: Mapping[str, str],
) -> dict[str, Any]:
    validate_execution_bundle(
        bundle.payload_path,
        expected=bundle.identity,
        review_inputs=review_inputs,
    )
    script_bytes = script.encode()
    if not 0 < bundle.payload_bytes <= MAX_ARCHIVE_BYTES or len(script_bytes) > MAX_SCRIPT_BYTES:
        _refuse("execution source transport exceeds byte bound")
    data = {
        "schema_version": 1,
        "payload_bytes": bundle.payload_bytes,
        "payload_sha256": bundle.payload_sha256,
        "script_bytes": len(script_bytes),
        "script_sha256": hashlib.sha256(script_bytes).hexdigest(),
        "identity": bundle.identity.model_dump(),
        "review_inputs": dict(review_inputs),
    }
    if len(canonical(data)) > MAX_HEADER_BYTES:
        _refuse("execution source header exceeds byte bound")
    return data


def execution_input(
    bundle: ExecutionSourceBundle,
    script: str,
    *,
    review_inputs: Mapping[str, str],
) -> bytes:
    header = execution_header(bundle, script, review_inputs=review_inputs)
    with bundle.payload_path.open("rb") as stream:
        raw = stream.read(MAX_ARCHIVE_BYTES + 1)
    if len(raw) != bundle.payload_bytes or hashlib.sha256(raw).hexdigest() != bundle.payload_sha256:
        _refuse("execution source transport bytes differ")
    return b"".join((canonical(header), b"\n", raw, script.encode()))


def projected_source_groups(
    bundle: ExecutionSourceBundle,
    *,
    review_inputs: Mapping[str, str],
) -> list[dict[str, str]]:
    """Split source bytes and compact identity metadata into bounded Secret groups."""

    header = execution_header(bundle, "", review_inputs=review_inputs)
    count = (bundle.payload_bytes + CHUNK_BYTES - 1) // CHUNK_BYTES
    metadata = {
        "sha256": bundle.payload_sha256,
        "size_bytes": bundle.payload_bytes,
        "count": count,
        "identity": header["identity"],
        "review_inputs": dict(review_inputs),
    }
    groups = [{"transport.json": base64.b64encode(canonical(metadata)).decode("ascii")}]
    digest = hashlib.sha256()
    with bundle.payload_path.open("rb") as stream:
        index = 0
        while chunk := stream.read(CHUNK_BYTES):
            if index % EXECUTION_CHUNKS_PER_SECRET == 0:
                groups.append({})
            groups[-1][f"chunk-{index:04}"] = base64.b64encode(chunk).decode("ascii")
            index += 1
            digest.update(chunk)
        if index != count or digest.hexdigest() != bundle.payload_sha256:
            _refuse("execution source bytes changed during projection")
    return groups
