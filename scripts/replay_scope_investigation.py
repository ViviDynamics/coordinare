"""Offline scope transcript audit; never upgrades old responses or executes sources."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from coordinare_service_inference.environment.evidence_safety import source_path
from coordinare_service_inference.environment.scope import ScopeAssessmentEnvelope
from pydantic import ValidationError

from coordinare.services.environment_function_wire import load_unique

MAX_FILES = 128
MAX_FILE_BYTES = 16*1024*1024
MAX_TOTAL_BYTES = 64*1024*1024


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('replay input exceeds bounded regular-file admission')
    value = load_unique(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('replay input must be a transcript object')
    return value


def replay(transcripts: Path) -> dict[str, Any]:
    """Count recorded source activity; requests and inspected citations stay distinct."""
    paths = sorted(transcripts.glob('chat-[0-9][0-9][0-9].json'))
    if not paths or len(paths) > MAX_FILES or sum(path.stat().st_size for path in paths) > MAX_TOTAL_BYTES:
        raise ValueError('replay transcript collection is empty or exceeds bounds')
    selected: set[str] = set()
    new_after_author: set[str] = set()
    structured: list[dict[str, Any]] = []
    authored = False
    new_since_review = False
    source_tools = post_author_tools = reviews = missing_protocol = premature_revisions = 0
    for path in paths:
        record = _read(path)
        response = record.get('response')
        response = load_unique(response) if isinstance(response, str) else response
        if not isinstance(response, dict):
            raise ValueError('replay response must be one logical envelope')
        if response.get('tool') in {'read_file', 'read_files'}:
            requested = response.get('paths', []) if response['tool'] == 'read_files' else [response.get('path')]
            if not isinstance(requested, list) or not 1 <= len(requested) <= 8:
                raise ValueError('replay source request is malformed')
            requested_paths = {source_path(value) for value in requested if isinstance(value, str)}
            if len(requested_paths) != len(requested):
                raise ValueError('replay source paths are malformed or repeated')
            new = requested_paths-selected
            source_tools += 1
            if authored:
                post_author_tools += 1
                new_after_author.update(new)
                new_since_review |= bool(new)
            selected.update(requested_paths)
        if 'scope' in response:
            authored = True
        if 'scope_assessment' in response:
            reviews += 1
            new_since_review = False
            try:
                assessment = ScopeAssessmentEnvelope.model_validate(response).scope_assessment
                for item in [*assessment.findings, *assessment.decisions]:
                    structured.extend(request.model_dump(mode='json') for request in item.inspection_requests)
            except ValidationError as exc:
                if any(error['type'] == 'missing' and error['loc'][-1:] == ('inspection_requests',) for error in exc.errors()):
                    missing_protocol += 1
        if 'scope_revision' in response and not new_since_review:
            premature_revisions += 1
    journal_path = transcripts/'environment-attempt.json'
    journal = _read(journal_path) if journal_path.exists() else {}
    result_path = transcripts/'result.json'
    recorded_result = _read(result_path) if result_path.exists() else {}
    reads = journal.get('reads')
    verification_reads = journal.get('verification_reads')
    held = recorded_result.get('reason_code')
    # Only trusted bounded categories escape; no raw messages/rationales or content.
    report = {'replayed_calls': len(paths), 'source_tool_calls': source_tools,
        'selected_source_count': len(selected), 'post_author_source_tools': post_author_tools,
        'new_selected_sources_after_author': sorted(new_after_author),
        'revisions_without_new_source_reads': premature_revisions,
        'source_review_responses': reviews, 'scope_reviews_missing_protocol_fields': missing_protocol,
        'structured_inspection_requests': structured,
        'corrected_protocol_rejection': 'inspection_requests_required' if missing_protocol else None,
        'recorded_source_reviews': len((journal.get('scope_findings') or {}).get('reviews', [])),
        'recorded_executable_reviews': len((journal.get('finding_ledger') or {}).get('reviews', [])),
        'recorded_fetch_count': reads if type(reads) is int and 0 <= reads <= 100000 else None,
        'recorded_verification_fetch_count': (verification_reads if type(verification_reads) is int
                                              and 0 <= verification_reads <= 10000000 else None),
        'recorded_hold': held if held in {'attempt_read_limit', 'attempt_deadline', 'attempt_coverage_limit', 'attempt_scope_revision_limit'} else None,
        'executed_project_commands': False, 'readiness_proven': False}
    if len(json.dumps(report).encode()) > 256*1024:
        raise ValueError('replay report exceeds metadata bound')
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--transcripts', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = replay(args.transcripts)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    sys.stdout.write(json.dumps({key: value for key, value in report.items()
                      if key not in {'structured_inspection_requests', 'new_selected_sources_after_author'}})+'\n')


if __name__ == '__main__':
    main()
