"""Complete independent mandatory evidence delivery with bounded conversations."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

from coordinare.services.environment_attempt import EnvironmentDiscoveryError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from coordinare.services.environment_inspection import SourceInspection


def source_evidence_refs(inspection: SourceInspection, evidence: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Native presentation references only exact bodies also required in the phase's source catalog.

    Only call for daemon-built source evidence fields. Investigation bindings/results remain unchanged;
    their nested evidence may reference the same canonical source. Legacy evidence retains its full body.
    """
    native = inspection.budget is not None and inspection.budget.agent_context is not None

    def reference(item: Mapping[str, Any]) -> dict[str, Any]:
        if native and set(item) == {'path', 'sha256', 'content'}:
            path, digest, content = item['path'], item['sha256'], item['content']
            if (isinstance(path, str) and isinstance(content, str)
                    and inspection.inputs.get(path) == digest
                    and inspection.source_files.get(path) == content
                    and hashlib.sha256(content.encode()).hexdigest() == digest):
                return {'path': path, 'sha256': digest, 'content_ref': path}
        return dict(item)

    return [{**item, 'evidence': reference(item['evidence'])}
            if set(item) == {'binding', 'result', 'evidence'} and isinstance(item['evidence'], dict)
            else reference(item) for item in evidence]


async def _prepare_mandatory_evidence(
    inspection: SourceInspection, expected_inputs: Mapping[str, str], *, phase: str,
) -> list[dict[str, str]]:
    budget = inspection.budget
    if budget is not None:
        budget.stage = f"{phase}_preparation"
        budget.mandatory_inputs_required = len(expected_inputs)
        budget.mandatory_inputs_delivered = 0
    evidence = []
    paths = sorted(expected_inputs)
    for offset in range(0, len(paths), 8):
        try:
            # The daemon's verification, not a model request: charged to verification_reads, not the model's cap.
            result = await inspection.read_files(paths[offset:offset + 8], verification=True)
        except EnvironmentDiscoveryError as exc:
            if exc.code != "environment_discovery_held":
                raise
            message = f"mandatory {phase} evidence unavailable or exceeds source bounds"
            raise EnvironmentDiscoveryError(message, code=f"{phase}_evidence_unavailable") from None
        for item in result["files"]:
            if item["sha256"] != expected_inputs[item["path"]]:
                message = f"mandatory {phase} evidence hash changed"
                raise EnvironmentDiscoveryError(message, code=f"{phase}_evidence_drift")
            evidence.append(item)
            if budget is not None:
                budget.mandatory_inputs_delivered += 1
    return evidence


def check_evidence_context(messages: list[dict[str, str]], *, max_bytes: int, phase: str) -> int:
    measured = len(json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    if measured > max_bytes:
        message = f"complete {phase} context exceeds configured byte limit"
        raise EnvironmentDiscoveryError(message, code=f"{phase}_context_limit")
    return measured


async def prepare_mandatory_evidence(
    inspection: SourceInspection, expected_inputs: Mapping[str, str], *, phase: str,
) -> list[dict[str, str]]:
    """Evidence preparation cannot outlive its existing phase or attempt bound."""
    budget = inspection.budget
    deadline = min(time.monotonic()+inspection.limits.phase_timeout_seconds,
                   budget.deadline if budget is not None else float('inf'))
    try:
        async with asyncio.timeout_at(deadline):
            return await _prepare_mandatory_evidence(inspection, expected_inputs, phase=phase)
    except TimeoutError:
        if budget is not None:
            budget.check()
        message = f'{phase} evidence preparation deadline exceeded'
        raise EnvironmentDiscoveryError(message, code=f'{phase}_preparation_deadline') from None


class AuthorEvidenceCache:
    """Attempt-local author bytes only; independent review always fetches afresh."""

    def __init__(self, *, source_commit: str, inventory_digest: str, max_bytes: int) -> None:
        if max_bytes < 1:
            message = 'author evidence cache requires a positive byte bound'
            raise ValueError(message)
        self.source_commit = source_commit
        self.inventory_digest = inventory_digest
        self.max_bytes = max_bytes
        self._entries: dict[str, tuple[str, str]] = {}

    def _check_identity(self, inspection: SourceInspection) -> None:
        budget = inspection.budget
        if (inspection.name in {'review', 'scope_review'} or inspection.inventory_digest != self.inventory_digest
                or budget is None or budget.scope_findings is None
                or budget.scope_findings.source_commit != self.source_commit):
            message = 'author cache source identity differs'
            raise EnvironmentDiscoveryError(message, code='scope_author_evidence_identity')

    def capture(self, inspection: SourceInspection) -> None:
        self._check_identity(inspection)
        candidate = dict(self._entries)
        for path, content in inspection.source_files.items():
            digest = hashlib.sha256(content.encode()).hexdigest()
            if (inspection.inputs.get(path) != digest or (path in candidate and candidate[path] != (content, digest))):
                message = 'author cache source hash differs'
                raise EnvironmentDiscoveryError(message, code='scope_author_evidence_drift')
            candidate[path] = (content, digest)
        if sum(len(content.encode()) for content, _digest in candidate.values()) > self.max_bytes:
            message = 'author cache source byte limit exceeded'
            raise EnvironmentDiscoveryError(message, code='scope_author_evidence_limit')
        self._entries = candidate

    def restore(self, inspection: SourceInspection, expected_inputs: Mapping[str, str]) -> list[dict[str, str]]:
        self._check_identity(inspection)
        evidence = []
        for path, expected in sorted(expected_inputs.items()):
            entry = self._entries.get(path)
            if entry is None:
                continue
            content, digest = entry
            if digest != expected or hashlib.sha256(content.encode()).hexdigest() != expected:
                message = 'author cache source hash differs'
                raise EnvironmentDiscoveryError(message, code='scope_author_evidence_drift')
            evidence.append(inspection.admit_content(path, content, fetched=False))
        return evidence
