"""Daemon bindings for explicit source investigation, separate from finding closure."""
from __future__ import annotations

from typing import TYPE_CHECKING

from coordinare_service_inference.environment.scope_investigation import (
    ScopeInspectionBinding,
    ScopeInspectionQueue,
)
from pydantic import ValidationError

from coordinare.services.environment_attempt import EnvironmentDiscoveryError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from coordinare_service_inference.environment.findings import FindingLedger
    from coordinare_service_inference.environment.scope import ScopeAssessmentProposal


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
