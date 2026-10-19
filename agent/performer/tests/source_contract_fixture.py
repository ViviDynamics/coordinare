"""Build actual pinned source identities for performer contract regressions."""
from __future__ import annotations

import copy
import hashlib
import shutil
import subprocess
from pathlib import Path

from coordinare_service_inference.environment.execution_source import SourceEntry, source_identity
from coordinare_service_inference.environment.models import EnvironmentManifest


def current_manifest_fixture(data: dict) -> EnvironmentManifest:
    """Explicit synthetic reviewed scope for protocol tests, never live validation.

    This constructs evidence at fixture setup only; admission still validates the
    complete real contract. Negative tests mutate it after setup.
    """
    from coordinare_service_inference.environment.findings import FindingLedger, FindingReview
    from coordinare_service_inference.environment.scope import (
        ScopeContract,
        ScopeCriterion,
        ScopeCriterionProposal,
        ScopeDefinition,
        criterion_id,
    )

    criterion = ScopeCriterionProposal(description='Synthetic source fixture obligation',
        sources=sorted(data['inputs']), applicability='required', rationale='Synthetic fixture source evidence',
        usability='Exercise the fixture source constraints through required consumer checks', runtime_needs=[])
    definition = ScopeDefinition(source_commit=data['source_commit'], inventory_digest=data['inventory_digest'],
        inputs=data['inputs'], criteria=[ScopeCriterion(id=criterion_id(criterion), **criterion.model_dump())])
    scope = ScopeContract(definition=definition, review_inputs=dict(data['inputs']), sources=sorted(data['inputs']),
        finding_ledger=FindingLedger(source_commit=data['source_commit'], reviews=[FindingReview(sequence=1,
            manifest_digest=definition.digest(), inputs=dict(data['inputs']), decisions=[], added_ids=[])]),
        finding_responses=[])
    checks = [item['id'] for item in data['checks'] if item.get('required', True) and not item.get('scope')]
    return EnvironmentManifest.model_validate({**data, 'schema_version': 3, 'policy_version': '353-scope-v1',
        'scope_contract': scope.model_dump(mode='json'), 'scope_contract_digest': scope.digest(),
        'criterion_checks': {definition.criteria[0].id: checks}})


def current_review_fixture(manifest: EnvironmentManifest, assessment: dict) -> dict:
    """Synthetic accepted protocol evidence, never used by live validation."""
    from coordinare_service_inference.environment.findings import FindingLedger, FindingReview

    ledger = FindingLedger(source_commit=manifest.source_commit, reviews=[FindingReview(sequence=1,
        manifest_digest=manifest.digest(), inputs=dict(assessment['inputs']), decisions=[], added_ids=[])])
    return {**assessment, 'schema_version': 2, 'policy_version': '353-findings-v1',
            'finding_ledger': ledger.model_dump(mode='json'), 'finding_ledger_digest': ledger.digest()}


def bind_checkout_contract(metadata: dict, root: Path) -> dict:
    """Keep real checkout preflight enabled while replacing external fixture setup."""
    values = copy.deepcopy(metadata)
    values.pop("environment_capabilities", None)  # Daemon target is not a performer Score field.
    executable = shutil.which('git')
    assert executable is not None
    def git(*args: str) -> str:
        return subprocess.check_output([
            executable, '-C', str(root), '-c', 'core.hooksPath=/dev/null',
            '-c', 'commit.gpgsign=false', '-c', 'user.name=Fixture',
            '-c', 'user.email=fixture@example.invalid', *args,
        ], text=True, stderr=subprocess.PIPE, timeout=10).strip()
    if not (root/'.git').exists():
        git('init')
        (root/'tools.plan').write_text('baseline')
        git('add', '--', 'tools.plan')
        git('commit', '-m', 'Pinned contract fixture')
    commit = git('rev-parse', 'HEAD')
    content = (root/'tools.plan').read_bytes()
    source = source_identity(commit, [SourceEntry(path='tools.plan', mode='100644', size=len(content),
        object_id=git('rev-parse', 'HEAD:tools.plan'))])
    manifest = current_manifest_fixture({**values['environment_manifest'], 'source_commit':commit,
        'inputs':{'tools.plan':hashlib.sha256(content).hexdigest()}})
    values.update(environment_manifest=manifest.model_dump(), environment_manifest_digest=manifest.digest(),
        environment_execution_source=source.model_dump())
    values['environment_assessment'].update(source_commit=commit, manifest_digest=manifest.digest(), inputs=dict(manifest.inputs))
    values['environment_assessment'] = current_review_fixture(manifest, values['environment_assessment'])
    return values
