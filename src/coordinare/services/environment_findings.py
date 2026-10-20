"""Daemon-owned transitions for independently adjudicated coverage findings."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

from coordinare_service_inference.environment.findings import (
    FindingLedger,
    FindingRecord,
    FindingReview,
)
from pydantic import ValidationError

from coordinare.services.environment_attempt import EnvironmentDiscoveryError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from coordinare_service_inference.environment.findings import FindingResponse
    from coordinare_service_inference.environment.models import (
        CoverageAssessmentProposal,
        EnvironmentManifest,
    )


MAX_FINDINGS = 32
CURRENT_REVIEW_VERSION = 2


class FindingValidationError(EnvironmentDiscoveryError):
    """Trusted diagnostic for correcting finding structure, never model prose."""

    def __init__(self, message: str, *, code: str, location: tuple[str, ...]) -> None:
        super().__init__(message, code=code)
        self.location = location


def refuse_finding(message: str, *, code: str = 'review_findings_invalid',
                   location: tuple[str, ...] = ()) -> NoReturn:
    raise FindingValidationError(message, code=code, location=location) from None


def _ids(manifest: EnvironmentManifest) -> set[str]:
    ids = {item.id for group in ('requirements', 'checks', 'install', 'services') for item in getattr(manifest, group)}
    for service in manifest.services:
        ids.update(command.id for command in (service.init, service.start, service.health, service.stop) if command is not None)
    return ids


def validate_finding_responses(responses: list[FindingResponse], *, ledger: FindingLedger,
                               inspected: Mapping[str, str], manifest: EnvironmentManifest) -> None:
    ids = [response.finding_id for response in responses]
    if (len(set(ids)) != len(ids) or not set(ledger.open_ids()) <= set(ids)
            or not set(ids) <= {record.id for record in ledger.records}):
        refuse_finding('finding response IDs must match established objections', code='finding_response_ids_invalid')
    if any(not set(response.sources) <= inspected.keys() or not set(response.affected_ids) <= _ids(manifest)
           for response in responses):
        refuse_finding('finding responses must address every open objection with read evidence', code='review_findings_invalid')


def apply_finding_review(ledger: FindingLedger, *, manifest: EnvironmentManifest,
                         proposal: CoverageAssessmentProposal, reviewer_inputs: Mapping[str, str]) -> FindingLedger:
    return apply_evidence_review(ledger, subject=ReviewSubject(manifest.source_commit, manifest.digest(), _ids(manifest)),
        proposal=proposal, reviewer_inputs=reviewer_inputs)


@dataclass(frozen=True)
class ReviewSubject:
    source_commit: str
    digest: str
    obligation_ids: set[str]


def _check_duplicate_decisions(ledger: FindingLedger, proposal: CoverageAssessmentProposal) -> None:
    retained = {record.id for record in ledger.records}
    for index, decision in enumerate(proposal.decisions):
        target = decision.duplicate_of
        if target is None:
            continue
        location = ('decisions', str(index))
        if decision.status != 'resolved':
            refuse_finding('A duplicate entry must use status resolved; its original finding remains independently blocking.',
                location=(*location, 'status'))
        if target not in retained or target == decision.finding_id:
            refuse_finding('duplicate_of must name another retained finding ID, never itself or a new finding.',
                location=(*location, 'duplicate_of'))


def apply_evidence_review(ledger: FindingLedger, *, subject: ReviewSubject, proposal: CoverageAssessmentProposal,
                          reviewer_inputs: Mapping[str, str]) -> FindingLedger:
    """Produce a new checked ledger; never mutate history during model correction."""
    source_commit, subject_digest, obligation_ids = subject.source_commit, subject.digest, subject.obligation_ids
    if ledger.source_commit != source_commit:
        refuse_finding('finding source identity changed', code='source_identity')
    _check_duplicate_decisions(ledger, proposal)
    if len(ledger.records) + len(proposal.findings) > MAX_FINDINGS:
        refuse_finding(f'At most {MAX_FINDINGS-len(ledger.records)} new findings fit; adjudicate retained IDs in decisions instead of re-adding them.',
            code='review_findings_limit', location=('findings',))
    for finding in proposal.findings:
        if not set(finding.sources) <= reviewer_inputs.keys():
            refuse_finding('finding cites unread evidence', code='review_citations_invalid')
        if not set(finding.affected_ids) <= obligation_ids:
            refuse_finding('finding refers to unknown manifest obligation', code='review_findings_invalid')
    for decision in proposal.decisions:
        if not set(decision.sources) <= reviewer_inputs.keys():
            refuse_finding('decision cites unread evidence', code='review_citations_invalid')
    records = [*ledger.records]
    added_ids = []
    for finding in proposal.findings:
        fid = f'F{len(records)+1:04}'
        added_ids.append(fid)
        records.append(FindingRecord(id=fid, proposal=finding, manifest_digest=subject_digest,
            source_commit=source_commit, inputs={path: reviewer_inputs[path] for path in finding.sources}))
    try:
        result = FindingLedger(source_commit=ledger.source_commit, records=records,
            reviews=[*ledger.reviews, FindingReview(sequence=len(ledger.reviews)+1,
                manifest_digest=subject_digest, inputs=dict(reviewer_inputs), decisions=proposal.decisions,
                added_ids=added_ids)])
    except ValidationError as exc:
        # Growth beyond trusted aggregate bounds is terminal; malformed model
        # decisions remain correctable within the existing schema allowance.
        if len(records) > MAX_FINDINGS or any('128 KiB' in str(error.get('msg', '')) for error in exc.errors()):
            refuse_finding('finding ledger limit exceeded', code='review_findings_limit')
        refuse_finding('finding adjudication is incomplete or invalid', code='review_findings_invalid')
    if (proposal.verdict == 'accept' and result.open_ids()) or (proposal.verdict == 'revise' and not result.open_ids()):
        refuse_finding('verdict contradicts explicit finding decisions', code='review_findings_invalid')
    categories = {record.id: record.proposal.category for record in result.records}
    if proposal.capability_unavailable and (proposal.verdict == 'accept'
            or not any(categories[fid] == 'capability' for fid in result.open_ids())):
        refuse_finding('capability_unavailable requires an open cited capability finding; '
                       'set it false when capability impossibility is not established, preserving all blocking findings',
                       location=('assessment', 'capability_unavailable'))
    return result


def coverage_summary(value: object) -> dict[str, object] | None:
    """Public projections must never expose raw findings, inputs or model prose."""
    from coordinare_service_inference.environment.findings import finding_summary
    from coordinare_service_inference.environment.models import CoverageAssessment

    try:
        assessment = CoverageAssessment.model_validate(value)
    except (ValueError, TypeError):
        return None
    current = assessment.schema_version == CURRENT_REVIEW_VERSION
    return {'schema_version': assessment.schema_version, 'manifest_digest': assessment.manifest_digest,
            'verdict': assessment.verdict if current else 'refresh_required',
            'rationale': ('Independent coverage review accepted; source identities validated by daemon.'
                          if current and assessment.verdict == 'accept' else
                          'Coverage review requires attention; inspect private evidence.'),
            **finding_summary(assessment.finding_ledger)}
