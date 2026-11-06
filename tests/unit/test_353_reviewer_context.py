"""Divided reviewers see what other parts already declared and resolved, so they never report it missing again.

Dettmore e2e-59: check-group reviewers were shown each service as lifecycle ids only and an always-empty
runtime_endpoints, and reported the declared postgres commands and forward missing in every coverage review; and a
scope shard reviewer, shown only the findings it owned, raised the Flow canvas row a third time after the merge
reviewer had resolved it.
"""
from __future__ import annotations

from coordinare_service_inference.environment.findings import (
    FindingDecisionProposal,
    FindingLedger,
    FindingProposal,
    FindingRecord,
    FindingReview,
)
from coordinare_service_inference.environment.models import EnvironmentManifest
from coordinare_service_inference.environment.runtime import (
    RuntimeEndpoint,
    RuntimePolicy,
    RuntimeSpec,
)

from coordinare.services.environment_discovery import review_summary
from coordinare.services.environment_shard_scope import resolved_elsewhere
from tests.unit.env_manifest_fixture import reviewed_metadata
from tests.unit.test_353_runtime_identity import policy_data

DIGEST = 'd' * 64
COMMIT = 'a' * 40


def test_part_reviewers_see_each_services_commands_and_the_declared_forwards():
    data = reviewed_metadata(checks=[{'id': 'suite', 'argv': ['quality-runner', 'suite']}])['environment_manifest']
    start = {'id': 'pg-start', 'argv': ['docker', 'run', '-d', '-p', '5432:5432', 'postgres:17']}
    data['services'] = [{'id': 'postgres', 'start': start, 'health': {'id': 'pg-health', 'argv': ['pg_isready']},
                         'stop': {'id': 'pg-stop', 'argv': ['docker', 'rm', '-f', 'pg']}}]
    manifest = EnvironmentManifest.model_validate(data)
    assert review_summary(manifest)['runtime_endpoints'] == []
    endpoint = RuntimeEndpoint(id='postgres', bind_address='127.0.0.1', guest_port=5432, outer_port=5432)
    runtime = RuntimeSpec(schema_version=1, policy=RuntimePolicy.model_validate(policy_data()), endpoints=[endpoint])
    summary = review_summary(manifest.model_copy(update={'runtime': runtime}))
    assert summary['services'][0]['start']['argv'] == start['argv']
    assert summary['readiness_ids'] == ['pg-health', 'pg-start']
    assert summary['runtime_endpoints'] == [endpoint.model_dump(mode='json')]


def _record(identifier: str, sources: list[str]) -> FindingRecord:
    return FindingRecord(id=identifier, manifest_digest=DIGEST, source_commit=COMMIT, inputs={},
                         proposal=FindingProposal(category='coverage', issue=f'{identifier} issue', sources=sources,
                                                  affected_ids=[]))


def test_a_shard_reviewer_sees_only_resolved_findings_about_its_files_that_it_does_not_own():
    records = [_record('F0001', ['.agents/test-commands.md']), _record('F0002', ['README.md']),
               _record('F0003', ['.agents/test-commands.md']), _record('F0004', ['.agents/test-commands.md'])]
    decisions = [FindingDecisionProposal(finding_id='F0001', status='resolved', rationale='row is stale', sources=['x']),
                 FindingDecisionProposal(finding_id='F0002', status='resolved', rationale='other file', sources=['x']),
                 FindingDecisionProposal(finding_id='F0003', status='open', rationale='still open', sources=['x']),
                 FindingDecisionProposal(finding_id='F0004', status='resolved', rationale='mine', sources=['x'])]
    # The helper only reads the ledger; its full evidence binding is the ledger tests' concern.
    review = FindingReview.model_construct(sequence=2, manifest_digest=DIGEST, inputs={}, decisions=decisions, added_ids=[])
    ledger = FindingLedger.model_construct(source_commit=COMMIT, records=records, reviews=[review])
    shown = resolved_elsewhere(ledger, owned=['F0004'], paths={'.agents/test-commands.md'})
    assert shown == [{'id': 'F0001', 'issue': 'F0001 issue', 'sources': ['.agents/test-commands.md'],
                      'resolution': 'row is stale'}]
    assert resolved_elsewhere(None, owned=[], paths={'README.md'}) == []
