"""Daemon bindings for explicit source investigation, separate from finding closure."""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import TYPE_CHECKING, Any

from coordinare_service_inference.environment.scope import canonical
from coordinare_service_inference.environment.scope_investigation import (
    ScopeInspectionBinding,
    ScopeInspectionQueue,
    ScopeInspectionResult,
)
from pydantic import ValidationError

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_findings import FindingValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from coordinare_service_inference.environment.findings import FindingLedger, FindingResponse
    from coordinare_service_inference.environment.scope import (
        ScopeAssessmentProposal,
        ScopeDefinition,
    )

    from coordinare.services.environment_inspection import SourceInspection


def bind_scope_requests(*, assessment: ScopeAssessmentProposal, ledger: FindingLedger,
                        scope_digest: str, inventory_digest: str,
                        retained: Sequence[ScopeInspectionBinding]) -> list[ScopeInspectionBinding]:
    """Validate the complete candidate queue before publishing ledger or requests."""
    latest = ledger.reviews[-1]
    if latest.manifest_digest != scope_digest or len(latest.added_ids) != len(assessment.findings):
        raise EnvironmentDiscoveryError('inspection review identity differs', code='scope_investigation_identity')
    requests = [(fid, finding.inspection_requests) for fid, finding in zip(latest.added_ids, assessment.findings, strict=True)]
    requests.extend((decision.finding_id, decision.inspection_requests) for decision in assessment.decisions)
    bindings = [*retained]
    for fid, proposed in requests:
        for request in proposed:
            bindings.append(ScopeInspectionBinding.bind(finding_id=fid, review_sequence=latest.sequence,
                scope_digest=scope_digest, source_commit=ledger.source_commit, inventory_digest=inventory_digest,
                request=request.model_dump(mode='json')))
    try:
        return list(ScopeInspectionQueue(bindings=bindings).bindings)
    except ValidationError:
        raise EnvironmentDiscoveryError('inspection queue exceeds supported bounds', code='scope_investigation_limit') from None


def _request_evidence(inspection: SourceInspection, binding: ScopeInspectionBinding) -> tuple[str, dict[str, Any]]:
    request = binding.request
    if request.operation == 'read_file':
        if request.path not in inspection.inventory:
            return 'absent', {'operation': 'read_file', 'path': request.path,
                              'inventory_digest': inspection.inventory_digest, 'status': 'absent'}
        if request.path not in inspection.source_files:
            raise EnvironmentDiscoveryError('investigation content is unavailable', code='scope_investigation_unavailable')
        return 'file', {'path': request.path, 'content': inspection.source_files[request.path],
                        'sha256': inspection.inputs[request.path]}
    try:
        return 'listing', {'path': request.path, 'cursor': request.cursor,
                           **inspection.list_directory(request.path, request.cursor)}
    except EnvironmentDiscoveryError as exc:
        # A file/directory mismatch or invalid cursor is not pinned absence.
        if (request.path in inspection.inventory or request.cursor != 0
                or request.path == '' or any(path.startswith(request.path+'/') for path in inspection.paths)):
            raise EnvironmentDiscoveryError('investigation directory page is unavailable',
                                            code='scope_investigation_unavailable') from exc
        return 'absent', {'operation': 'list_files', 'path': request.path, 'cursor': request.cursor,
                          'inventory_digest': inspection.inventory_digest, 'status': 'absent'}


def _bound_result(inspection: SourceInspection, binding: ScopeInspectionBinding) -> tuple[ScopeInspectionResult, dict[str, Any]]:
    if binding.inventory_digest != inspection.inventory_digest:
        raise EnvironmentDiscoveryError('investigation inventory differs', code='scope_investigation_identity')
    if (inspection.budget is not None and inspection.budget.scope_findings is not None
            and binding.source_commit != inspection.budget.scope_findings.source_commit):
        raise EnvironmentDiscoveryError('investigation commit differs', code='scope_investigation_identity')
    status, evidence = _request_evidence(inspection, binding)
    result = ScopeInspectionResult(binding_id=binding.id, status=status,
        evidence_digest=hashlib.sha256(canonical(evidence)).hexdigest(),
        file_sha256=evidence['sha256'] if status == 'file' else None)
    return result, evidence


async def investigate_scope_requests(inspection: SourceInspection,
                                    bindings: Sequence[ScopeInspectionBinding]) -> list[ScopeInspectionResult]:
    """Read only pinned evidence; no executable/project commands are available here."""
    budget = inspection.budget
    deadline = min(time.monotonic()+inspection.limits.phase_timeout_seconds,
                   budget.deadline if budget is not None else float('inf'))
    results = []
    try:
        async with asyncio.timeout_at(deadline):
            for binding in bindings:
                if binding.inventory_digest != inspection.inventory_digest:
                    raise EnvironmentDiscoveryError('investigation inventory differs', code='scope_investigation_identity')
                request = binding.request
                if (request.operation == 'read_file' and request.path in inspection.inventory
                        and request.path not in inspection.source_files):
                    try:
                        await inspection.read_files([request.path])
                    except EnvironmentDiscoveryError as exc:
                        if exc.code not in {'environment_discovery_held', 'source_identity'}:
                            raise
                        raise EnvironmentDiscoveryError('required source investigation could not read pinned content',
                                                        code='scope_investigation_unavailable') from None
                result, _ = _bound_result(inspection, binding)
                results.append(result)
                if budget is not None:
                    if any(item.binding_id == result.binding_id for item in budget.scope_investigation_results):
                        raise EnvironmentDiscoveryError('investigation result already recorded', code='scope_investigation_identity')
                    budget.scope_investigation_results.append(result)
                    await budget.flush()
    except TimeoutError:
        if budget is not None:
            budget.check()
        raise EnvironmentDiscoveryError('source investigation deadline exceeded', code='scope_investigation_deadline') from None
    return results


def investigation_evidence(*, inspection: SourceInspection, bindings: Sequence[ScopeInspectionBinding],
                           results: Sequence[ScopeInspectionResult]) -> list[dict[str, Any]]:
    """Deliver exact result/content pairs; recorded completion never invents bytes."""
    recorded = {result.binding_id: result for result in results}
    if len(recorded) != len(results):
        raise EnvironmentDiscoveryError('investigation result identities repeat', code='scope_investigation_identity')
    evidence = []
    for binding in bindings:
        result, payload = _bound_result(inspection, binding)
        if recorded.get(binding.id) != result:
            raise EnvironmentDiscoveryError('investigation result is missing or differs', code='scope_investigation_identity')
        evidence.append({'binding': binding.model_dump(mode='json'), 'result': result.model_dump(mode='json'), 'evidence': payload})
    return evidence


def validate_investigation_responses(*, bindings: Sequence[ScopeInspectionBinding],
                                    results: Sequence[ScopeInspectionResult], responses: Sequence[FindingResponse],
                                    inputs: Mapping[str, str]) -> None:
    recorded = {result.binding_id: result for result in results}
    answered = {response.finding_id: response for response in responses}
    if len(recorded) != len(results):
        raise EnvironmentDiscoveryError('investigation result identities repeat', code='scope_investigation_identity')
    for binding in bindings:
        result = recorded.get(binding.id)
        if result is None:
            raise EnvironmentDiscoveryError('investigation is incomplete', code='scope_investigation_identity')
        if result.status == 'file':
            response = answered.get(binding.finding_id)
            if (binding.request.operation != 'read_file' or response is None
                    or binding.request.path not in response.sources
                    or inputs.get(binding.request.path) != result.file_sha256):
                raise FindingValidationError('finding response must cite requested inspected file',
                    code='scope_investigation_response_invalid', location=('finding_responses',))


def validate_investigation_history(*, bindings: Sequence[ScopeInspectionBinding],
                                   results: Sequence[ScopeInspectionResult], ledger: FindingLedger | None,
                                   definition: ScopeDefinition | None, inputs: Mapping[str, str]) -> None:
    """Bind persisted history to its originating review, never the latest scope."""
    try:
        checked = ScopeInspectionQueue.model_validate({'bindings': [item.model_dump(mode='json') for item in bindings]})
        parsed = [ScopeInspectionResult.model_validate(item.model_dump(mode='json')) for item in results]
        if len(canonical({'bindings': [item.model_dump(mode='json') for item in checked.bindings],
                          'results': [item.model_dump(mode='json') for item in parsed]})) > 256*1024:
            raise ValueError('investigation metadata exceeds aggregate bound')
        by_id = {item.id: item for item in checked.bindings}
        if len({result.binding_id for result in parsed}) != len(parsed):
            raise ValueError('investigation result identities repeat')
        for binding in checked.bindings:
            if (ledger is None or definition is None or binding.source_commit != ledger.source_commit
                    or binding.inventory_digest != definition.inventory_digest
                    or binding.review_sequence > len(ledger.reviews)):
                raise ValueError('investigation origin differs')
            origin = ledger.reviews[binding.review_sequence-1]
            eligible = {*origin.added_ids, *(item.finding_id for item in origin.decisions if item.status == 'open')}
            if binding.scope_digest != origin.manifest_digest or binding.finding_id not in eligible:
                raise ValueError('investigation differs from originating review')
        for result in parsed:
            binding = by_id.get(result.binding_id)
            if binding is None:
                raise ValueError('investigation result has no request')
            request = binding.request
            if ((result.status == 'file' and (request.operation != 'read_file'
                    or inputs.get(request.path) != result.file_sha256))
                    or (result.status == 'listing' and request.operation != 'list_files')
                    or (result.status == 'absent' and request.path in inputs)):
                raise ValueError('investigation result differs from retained source')
    except (ValidationError, ValueError, TypeError):
        raise EnvironmentDiscoveryError('investigation checkpoint identity is invalid', code='scope_investigation_identity') from None
