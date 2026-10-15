"""Validate the bounded claim summary before storing any Kubernetes provenance."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from coordinare_service_inference.cache_provenance import (
    MAX_PATH_BYTES,
    MAX_RESULT_BYTES,
    MAX_SCRIPT_BYTES,
    RESULT_SENTINEL,
)

from coordinare.lib.redaction import redact_command_text
from coordinare.services.env_provenance import ProvenanceTransientError
from coordinare.services.kubernetes_verifier import VERIFIER_LOG_TAIL_CHARS, WALK_DIAGNOSTIC_PREFIX

if TYPE_CHECKING:
    from coordinare.services.env_provenance import ProvenanceLimits

_DIGEST = re.compile(r"[0-9a-f]{64}")
_RESULT_KEYS = {"content_digest", "stat_digest", "entry_count", "total_bytes", "inputs"}
_INPUT_KEYS = {"activate_sh", "services_start_sh", "services_health_sh", "debs"}


def validate_summary(doc: Any, activate: str, limits: ProvenanceLimits) -> dict[str, Any]:
    """Refuse malformed counters, digests, scripts, paths or unbounded consumer inputs."""
    if not isinstance(doc, dict) or set(doc) != _RESULT_KEYS:
        message = "verifier provenance has invalid fields"
        raise ValueError(message)
    if len(json.dumps(doc).encode("utf-8")) > MAX_RESULT_BYTES:
        message = "verifier provenance exceeds the result bound"
        raise ValueError(message)
    for key in ("content_digest", "stat_digest"):
        if not isinstance(doc[key], str) or _DIGEST.fullmatch(doc[key]) is None:
            message = "verifier provenance has an invalid digest"
            raise ValueError(message)
    for key, bound in (("entry_count", limits.max_entries), ("total_bytes", limits.max_bytes)):
        if type(doc[key]) is not int or not 0 <= doc[key] <= bound:
            message = f"verifier provenance has an invalid {key}"
            raise ValueError(message)
    _validate_inputs(doc["inputs"], activate, doc["entry_count"])
    return doc


def _validate_inputs(inputs: Any, activate: str, entry_count: int) -> None:
    if not isinstance(inputs, dict) or set(inputs) != _INPUT_KEYS:
        message = "verifier provenance has invalid consumer inputs"
        raise ValueError(message)
    if inputs["activate_sh"] != activate:
        message = "verifier activation differs from the daemon-rendered text"
        raise ValueError(message)
    for key in ("activate_sh", "services_start_sh", "services_health_sh"):
        text = inputs[key]
        if text is not None and (not isinstance(text, str) or len(text.encode("utf-8")) > MAX_SCRIPT_BYTES):
            message = "verifier provenance has an invalid script"
            raise ValueError(message)
    debs = inputs["debs"]
    if not isinstance(debs, dict) or len(debs) > entry_count:
        message = "verifier provenance has an invalid deb list"
        raise ValueError(message)
    for rel, digest in debs.items():
        if (not isinstance(rel, str) or not rel.startswith("debs/") or not rel.endswith(".deb")
            or any(part in ("", ".", "..") for part in rel.split("/"))
            or len(rel.encode("utf-8")) > MAX_PATH_BYTES or "\0" in rel
            or not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None):
            message = "verifier provenance has an invalid deb"
            raise ValueError(message)


def parse_summary(
    logs: str, activate: str, limits: ProvenanceLimits, *, inputs_current: bool = True,
) -> dict[str, Any]:
    """Read exactly one sentinel line. No manifest or unbounded log is accepted."""
    if not inputs_current:
        message = "bootstrap inputs changed while verification was running; retry"
        raise ProvenanceTransientError(message)
    if len(logs.encode("utf-8")) > MAX_RESULT_BYTES + VERIFIER_LOG_TAIL_CHARS:
        message = "verifier log exceeds the result bound"
        raise ValueError(message)
    nonempty = [line for line in logs.splitlines() if line.strip()]
    if not nonempty or not nonempty[-1].startswith(RESULT_SENTINEL):
        message = "verifier final provenance sentinel is missing; retry or upgrade the performer image"
        diagnostics = " ".join(line for line in nonempty if line.startswith(WALK_DIAGNOSTIC_PREFIX))
        if diagnostics:
            message += ": " + redact_command_text(diagnostics)[-VERIFIER_LOG_TAIL_CHARS:]
        raise ProvenanceTransientError(message)
    lines = [line[len(RESULT_SENTINEL):] for line in nonempty if line.startswith(RESULT_SENTINEL)]
    if logs.count(RESULT_SENTINEL) != 1 or len(lines) != 1 or len(lines[0].encode("utf-8")) > MAX_RESULT_BYTES:
        message = "verifier provenance is missing, duplicated or exceeds the result bound; upgrade the performer image"
        raise ValueError(message)
    doc = json.loads(lines[0])
    if isinstance(doc, dict) and "error" in doc:
        if doc.get("transient") is True:
            message = "verifier provenance walk had an infrastructure failure"
            raise ProvenanceTransientError(message)
        message = f"verifier provenance walk refused: {str(doc['error'])[:MAX_PATH_BYTES]}"
        raise ValueError(message)
    return validate_summary(doc, activate, limits)
