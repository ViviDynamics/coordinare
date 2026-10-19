"""Complete independent mandatory evidence delivery with bounded conversations."""
from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

from coordinare.services.environment_attempt import EnvironmentDiscoveryError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from coordinare.services.environment_inspection import SourceInspection


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
            result = await inspection.read_files(paths[offset:offset + 8])
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
