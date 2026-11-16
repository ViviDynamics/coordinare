"""Pure, shape-guarded review diagnostics; final domain admission stays authoritative."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from coordinare.services.agent_runtime.models import AdmissionIssue

if TYPE_CHECKING:
    from collections.abc import Mapping

# This policy is checkpoint-bound by the phase adapter. Changing the selected
# fields or their meaning requires a version change, even if JSON Schema is stable.
REVIEW_DIAGNOSTICS_POLICY = 'review_sources_utf8_target_and_read_completion_v3'
REVIEW_ENVELOPES = frozenset({'scope_assessment', 'assessment'})
# FindingEvidence.bounded_text and ScopeAssessmentProposal share this domain bound.
REVIEW_TEXT_MAX_UTF8_BYTES = 2048
# nare-native-004: told only to rewrite within the bound, a shard reviewer trimmed 22-84 bytes per correction
# (2252, 2200, 2178, 2094, 2076, 2052) until its shared correction allowance was spent. A concrete target with margin
# lets one rewrite land; the bound and the allowance are unchanged.
REVIEW_TEXT_TARGET_UTF8_BYTES = REVIEW_TEXT_MAX_UTF8_BYTES * 4 // 5
UTF8_BYTES_PER_WORD = 6


def review_response_diagnostics(candidate: object, *, envelope: str,
                                inspected: Mapping[str, str]) -> list[AdmissionIssue]:
    """Report independent citation/byte faults without parsing or normalizing a model.

    Wrong-shaped values belong to the strict schema. No arbitrary candidate text
    is returned: diagnostics carry trusted field paths, indices, and counts.
    """
    if envelope not in REVIEW_ENVELOPES or not isinstance(candidate, dict):
        return []
    assessment = candidate.get(envelope)
    if not isinstance(assessment, dict):
        return []
    records: list[tuple[tuple[str | int, ...], dict[str, Any]]] = [((envelope,), assessment)]
    for field in ('findings', 'decisions'):
        items = assessment.get(field)
        if isinstance(items, list):
            records.extend(((envelope, field, index), item) for index, item in enumerate(items)
                           if isinstance(item, dict))
    issues = []
    for location, record in records:
        sources = record.get('sources')
        if isinstance(sources, list):
            invalid = [index for index, path in enumerate(sources) if isinstance(path, str) and path not in inspected]
            if invalid:
                code = 'scope_citations_invalid' if envelope == 'scope_assessment' else 'review_citations_invalid'
                details = {'code': code, 'invalid_source_indices': invalid, 'inspected_files': sorted(inspected)[:200],
                           'more_inspected_files': max(0, len(inspected) - 200),
                           'guidance': 'Read each uninspected cited file with read_source({"path":...}) before '
                           'answering. Preserve the evidence needed for the judgment and return the complete envelope.'}
                issues.append(AdmissionIssue(code, json.dumps(details, sort_keys=True), (*location, 'sources')))
        bounded_fields = (('rationale',) if envelope == 'scope_assessment' else ()) if len(location) == 1 else (
            ('issue',) if location[1] == 'findings' else ('rationale',))
        for field in bounded_fields:
            value = record.get(field)
            if not isinstance(value, str):
                continue
            try:
                byte_length = len(value.encode('utf-8'))
            except UnicodeEncodeError:
                issues.append(AdmissionIssue('response_utf8_invalid', 'Use valid Unicode text.', (*location, field)))
                continue
            if byte_length > REVIEW_TEXT_MAX_UTF8_BYTES:
                excess = byte_length - REVIEW_TEXT_MAX_UTF8_BYTES
                details = {'max_utf8_bytes': REVIEW_TEXT_MAX_UTF8_BYTES, 'actual_utf8_bytes': byte_length,
                           'actual_characters': len(value), 'remove_at_least_utf8_bytes': excess,
                           'target_utf8_bytes': REVIEW_TEXT_TARGET_UTF8_BYTES,
                           'guidance': f'Shorten this summary by at least {excess} UTF-8 bytes, and aim for '
                           f'{REVIEW_TEXT_TARGET_UTF8_BYTES} bytes or fewer (about '
                           f'{REVIEW_TEXT_TARGET_UTF8_BYTES // UTF8_BYTES_PER_WORD} words) so one rewrite is enough: '
                           'trimming a few words at a time spends a correction each. Keep its meaning, and keep '
                           'findings, decisions, sources and all other fields unchanged.'}
                issues.append(AdmissionIssue('response_utf8_limit', json.dumps(details, sort_keys=True),
                                             (*location, field)))
    return issues
