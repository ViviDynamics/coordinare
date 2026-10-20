"""Pinned absence points to same-named inventory candidates; candidates are hints, never citations."""
from __future__ import annotations

import hashlib

import pytest
from coordinare_service_inference.environment.scope_investigation import (
    ScopeInspectionBinding,
    ScopeInspectionResult,
)
from pydantic import ValidationError

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_inspection import SourceInspection
from coordinare.services.environment_scope import canonical
from coordinare.services.environment_scope_investigation import (
    absence_candidates,
    investigate_scope_requests,
    investigation_evidence,
)

COMMIT = 'a'*40


def binding(inspection, request):
    return ScopeInspectionBinding.bind(finding_id='F0001', review_sequence=1, scope_digest='1'*64,
        source_commit=COMMIT, inventory_digest=inspection.inventory_digest, request=request)


def inspection(paths):
    async def fetch(path):
        return 'content'
    return SourceInspection(paths, fetch, DiscoveryLimits())


def test_nested_relative_reference_ranks_real_path_first():
    paths = ['other/gen.mjs', 'pkg/scripts/gen.mjs', 'pkg/scripts/other.mjs']
    assert absence_candidates('scripts/gen.mjs', paths, directories=False) == (['pkg/scripts/gen.mjs', 'other/gen.mjs'], 0)


def test_common_name_caps_at_eight_with_exact_overflow():
    paths = [f'd{i:02}/README.md' for i in range(39)] + ['docs/tests/README.md']
    candidates, more = absence_candidates('tests/README.md', paths, directories=False)
    assert candidates[0] == 'docs/tests/README.md' and len(candidates) == 8 and more == 32
    assert candidates[1:] == sorted(candidates[1:])


def test_missing_directory_candidates_are_directories():
    paths = ['a/tools/run.sh', 'b/tools/x/y.txt', 'tools.md']
    assert absence_candidates('tools', paths, directories=True) == (['a/tools', 'b/tools'], 0)
    assert absence_candidates('tools', paths, directories=False) == ([], 0)


@pytest.mark.asyncio
async def test_absence_evidence_delivers_candidates_and_digest_binds_them():
    source = inspection(['pkg/scripts/gen.mjs', 'guide'])
    item = binding(source, {'operation': 'read_file', 'path': 'scripts/gen.mjs'})
    [result] = await investigate_scope_requests(source, [item])
    assert result.status == 'absent' and result.candidates == ['pkg/scripts/gen.mjs'] and result.more_candidates == 0
    [evidence] = investigation_evidence(inspection=source, bindings=[item], results=[result])
    assert evidence['evidence']['candidates'] == ['pkg/scripts/gen.mjs']
    assert 'content' not in evidence['evidence']
    tampered = result.model_copy(update={'candidates': ['guide']})
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        investigation_evidence(inspection=source, bindings=[item], results=[tampered])
    assert failure.value.code == 'scope_investigation_identity'


def test_pre_amendment_absence_result_still_matches():
    source = inspection(['pkg/scripts/gen.mjs'])
    item = binding(source, {'operation': 'read_file', 'path': 'scripts/gen.mjs'})
    legacy_payload = {'operation': 'read_file', 'path': 'scripts/gen.mjs',
                      'inventory_digest': source.inventory_digest, 'status': 'absent'}
    legacy = ScopeInspectionResult(binding_id=item.id, status='absent',
        evidence_digest=hashlib.sha256(canonical(legacy_payload)).hexdigest())
    assert legacy.candidates is None
    [evidence] = investigation_evidence(inspection=source, bindings=[item], results=[legacy])
    assert 'candidates' not in evidence['evidence']


def test_candidates_only_on_absent():
    with pytest.raises(ValidationError):
        ScopeInspectionResult(binding_id='I'+'0'*64, status='file', evidence_digest='0'*64,
                              file_sha256='0'*64, candidates=['a'])
    with pytest.raises(ValidationError):
        ScopeInspectionResult(binding_id='I'+'0'*64, status='absent', evidence_digest='0'*64,
                              candidates=[f'p{i}' for i in range(9)])
