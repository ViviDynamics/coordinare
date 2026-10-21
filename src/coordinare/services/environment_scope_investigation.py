"""Daemon bindings for explicit source investigation, separate from finding closure."""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import TYPE_CHECKING, Any, NoReturn

from coordinare_service_inference.environment.scope import SourceGapDecision, canonical
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
        message = 'inspection review identity differs'
        raise EnvironmentDiscoveryError(message, code='scope_investigation_identity')
    requests = [(fid, finding.inspection_requests) for fid, finding in zip(latest.added_ids, assessment.findings, strict=True)]
    requests.extend((decision.finding_id, decision.inspection_requests) for decision in assessment.decisions
                    if isinstance(decision, SourceGapDecision))
    bindings = [*retained]
    for fid, proposed in requests:
        bindings.extend(ScopeInspectionBinding.bind(finding_id=fid, review_sequence=latest.sequence,
            scope_digest=scope_digest, source_commit=ledger.source_commit, inventory_digest=inventory_digest,
            request=request.model_dump(mode='json')) for request in proposed)
    try:
        return list(ScopeInspectionQueue(bindings=bindings).bindings)
    except ValidationError:
        message = 'inspection queue exceeds supported bounds'
        raise EnvironmentDiscoveryError(message, code='scope_investigation_limit') from None


MAX_ABSENCE_CANDIDATES = 8


def absence_candidates(path: str, paths: Sequence[str], *, directories: bool) -> tuple[list[str], int]:
    """Same final path component, most shared trailing components first; path text only."""
    if directories:
        pool = {'/'.join(item.split('/')[:index]) for item in paths for index in range(1, item.count('/')+1)}
    else:
        pool = set(paths)
    wanted = path.split('/')
    def shared(candidate: str) -> int:
        count = 0
        for left, right in zip(reversed(wanted), reversed(candidate.split('/')), strict=False):
            if left != right:
                break
            count += 1
        return count
    matches = sorted((item for item in pool if item != path and item.split('/')[-1] == wanted[-1]),
                     key=lambda item: (-shared(item), len(item), item))
    return matches[:MAX_ABSENCE_CANDIDATES], max(0, len(matches)-MAX_ABSENCE_CANDIDATES)


def _absent(inspection: SourceInspection, payload: dict[str, Any], *, directories: bool, candidates: bool) -> tuple[str, dict[str, Any]]:
    if candidates:
        names, more = absence_candidates(payload['path'], inspection.paths, directories=directories)
        payload = {**payload, 'candidates': names, 'more_candidates': more}
    return 'absent', payload


def _request_evidence(inspection: SourceInspection, binding: ScopeInspectionBinding, *,
                      candidates: bool = True) -> tuple[str, dict[str, Any]]:
    request = binding.request
    if request.operation == 'read_file':
        policy = inspection.policy_status(request.path) if request.path in inspection.inventory else 'readable'
        if policy != 'readable':
            entry = inspection.entries[request.path] if inspection.entries is not None else None
            return policy, {'operation': 'read_file', 'path': request.path, 'inventory_digest': inspection.inventory_digest,
                            'status': policy, 'size': entry.size if entry is not None else None}
        if request.path not in inspection.inventory:
            return _absent(inspection, {'operation': 'read_file', 'path': request.path,
                'inventory_digest': inspection.inventory_digest, 'status': 'absent'}, directories=False, candidates=candidates)
        if request.path not in inspection.source_files:
            message = 'investigation content is unavailable'
            raise EnvironmentDiscoveryError(message, code='scope_investigation_unavailable')
        return 'file', {'path': request.path, 'content': inspection.source_files[request.path],
                        'sha256': inspection.inputs[request.path]}
    try:
        return 'listing', {'path': request.path, 'cursor': request.cursor,
                           **inspection.list_directory(request.path, request.cursor)}
    except EnvironmentDiscoveryError as exc:
        # A file/directory mismatch or invalid cursor is not pinned absence.
        if (request.path in inspection.inventory or request.cursor != 0
                or request.path == '' or any(path.startswith(request.path+'/') for path in inspection.paths)):
            message = 'investigation directory page is unavailable'
            raise EnvironmentDiscoveryError(message,
                                            code='scope_investigation_unavailable') from exc
        return _absent(inspection, {'operation': 'list_files', 'path': request.path, 'cursor': request.cursor,
            'inventory_digest': inspection.inventory_digest, 'status': 'absent'}, directories=True, candidates=candidates)


def _bound_result(inspection: SourceInspection, binding: ScopeInspectionBinding, *,
                  candidates: bool = True) -> tuple[ScopeInspectionResult, dict[str, Any]]:
    if binding.inventory_digest != inspection.inventory_digest:
        message = 'investigation inventory differs'
        raise EnvironmentDiscoveryError(message, code='scope_investigation_identity')
    if (inspection.budget is not None and inspection.budget.scope_findings is not None
            and binding.source_commit != inspection.budget.scope_findings.source_commit):
        message = 'investigation commit differs'
        raise EnvironmentDiscoveryError(message, code='scope_investigation_identity')
    status, evidence = _request_evidence(inspection, binding, candidates=candidates)
    result = ScopeInspectionResult(binding_id=binding.id, status=status,
        evidence_digest=hashlib.sha256(canonical(evidence)).hexdigest(),
        file_sha256=evidence['sha256'] if status == 'file' else None,
        candidates=evidence.get('candidates'), more_candidates=evidence.get('more_candidates', 0))
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
                    message = 'investigation inventory differs'
                    raise EnvironmentDiscoveryError(message, code='scope_investigation_identity')
                request = binding.request
                if (request.operation == 'read_file' and request.path in inspection.inventory
                        and inspection.policy_status(request.path) == 'readable'
                        and request.path not in inspection.source_files):
                    try:
                        await inspection.read_files([request.path])
                    except EnvironmentDiscoveryError as exc:
                        if exc.code not in {'environment_discovery_held', 'source_identity'}:
                            raise
                        message = 'required source investigation could not read pinned content'
                        raise EnvironmentDiscoveryError(message,
                                                        code='scope_investigation_unavailable') from None
                result, _ = _bound_result(inspection, binding)
                results.append(result)
                if budget is not None:
                    if any(item.binding_id == result.binding_id for item in budget.scope_investigation_results):
                        message = 'investigation result already recorded'
                        raise EnvironmentDiscoveryError(message, code='scope_investigation_identity')
                    budget.scope_investigation_results.append(result)
                    await budget.flush()
    except TimeoutError:
        if budget is not None:
            budget.check()
        message = 'source investigation deadline exceeded'
        raise EnvironmentDiscoveryError(message, code='scope_investigation_deadline') from None
    return results


def investigation_evidence(*, inspection: SourceInspection, bindings: Sequence[ScopeInspectionBinding],
                           results: Sequence[ScopeInspectionResult]) -> list[dict[str, Any]]:
    """Deliver exact result/content pairs; recorded completion never invents bytes."""
    recorded = {result.binding_id: result for result in results}
    if len(recorded) != len(results):
        message = 'investigation result identities repeat'
        raise EnvironmentDiscoveryError(message, code='scope_investigation_identity')
    evidence = []
    for binding in bindings:
        prior = recorded.get(binding.id)
        # Results recorded before candidates existed are re-derived without them, never upgraded.
        result, payload = _bound_result(inspection, binding, candidates=prior is None or prior.candidates is not None
                                        or prior.status != 'absent')
        if prior != result:
            message = 'investigation result is missing or differs'
            raise EnvironmentDiscoveryError(message, code='scope_investigation_identity')
        evidence.append({'binding': binding.model_dump(mode='json'), 'result': result.model_dump(mode='json'), 'evidence': payload})
    return evidence


class ScopeInvestigationResponseError(FindingValidationError):
    """All omitted requested-file citations, from trusted inspection bindings only."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__('finding responses must cite requested inspected files',
                         code='scope_investigation_response_invalid', location=('finding_responses',))
        self.errors = errors


def validate_investigation_responses(*, bindings: Sequence[ScopeInspectionBinding],
                                    results: Sequence[ScopeInspectionResult], responses: Sequence[FindingResponse],
                                    inputs: Mapping[str, str]) -> None:
    recorded = {result.binding_id: result for result in results}
    answered = {response.finding_id: response for response in responses}
    indices = {response.finding_id: str(index) for index, response in enumerate(responses)}
    errors = []
    if len(recorded) != len(results):
        message = 'investigation result identities repeat'
        raise EnvironmentDiscoveryError(message, code='scope_investigation_identity')
    for binding in bindings:
        result = recorded.get(binding.id)
        if result is None:
            message = 'investigation is incomplete'
            raise EnvironmentDiscoveryError(message, code='scope_investigation_identity')
        if result.status == 'file':
            response = answered.get(binding.finding_id)
            if (binding.request.operation != 'read_file' or response is None
                    or binding.request.path not in response.sources
                    or inputs.get(binding.request.path) != result.file_sha256):
                errors.append({'code': 'scope_investigation_response_invalid',
                    'location': ['finding_responses', indices.get(binding.finding_id, ''), 'sources'],
                    'finding_id': binding.finding_id, 'required_source': binding.request.path,
                    'guidance': 'Cite this exact requested inspected file in the corresponding finding response. '
                                'Keep the proposed scope_revision operations; correct all listed citations together. '
                                'Investigation does not close the finding or establish readiness.'})
    if errors:
        raise ScopeInvestigationResponseError(errors)


def _invalid_history(message: str) -> NoReturn:
    raise ValueError(message)


def validate_investigation_history(*, bindings: Sequence[ScopeInspectionBinding],
                                   results: Sequence[ScopeInspectionResult], ledger: FindingLedger | None,
                                   definition: ScopeDefinition | None, inputs: Mapping[str, str]) -> None:
    """Bind persisted history to its originating review, never the latest scope."""
    try:
        checked = ScopeInspectionQueue.model_validate({'bindings': [item.model_dump(mode='json') for item in bindings]})
        parsed = [ScopeInspectionResult.model_validate(item.model_dump(mode='json')) for item in results]
        if len(canonical({'bindings': [item.model_dump(mode='json') for item in checked.bindings],
                          'results': [item.model_dump(mode='json') for item in parsed]})) > 256*1024:
            _invalid_history('investigation metadata exceeds aggregate bound')
        by_id = {item.id: item for item in checked.bindings}
        if len({result.binding_id for result in parsed}) != len(parsed):
            _invalid_history('investigation result identities repeat')
        for binding in checked.bindings:
            if (ledger is None or definition is None or binding.source_commit != ledger.source_commit
                    or binding.inventory_digest != definition.inventory_digest
                    or binding.review_sequence > len(ledger.reviews)):
                _invalid_history('investigation origin differs')
            origin = ledger.reviews[binding.review_sequence-1]
            eligible = {*origin.added_ids, *(item.finding_id for item in origin.decisions if item.status == 'open')}
            if binding.scope_digest != origin.manifest_digest or binding.finding_id not in eligible:
                _invalid_history('investigation differs from originating review')
        for result in parsed:
            binding = by_id.get(result.binding_id)
            if binding is None:
                _invalid_history('investigation result has no request')
            request = binding.request
            if ((result.status == 'file' and (request.operation != 'read_file'
                    or inputs.get(request.path) != result.file_sha256))
                    or (result.status == 'listing' and request.operation != 'list_files')
                    or (result.status in {'absent', 'too_large', 'link'} and request.path in inputs)
                    or (result.status in {'too_large', 'link'} and request.operation != 'read_file')):
                _invalid_history('investigation result differs from retained source')
    except (ValidationError, ValueError, TypeError):
        message = 'investigation checkpoint identity is invalid'
        raise EnvironmentDiscoveryError(message, code='scope_investigation_identity') from None
