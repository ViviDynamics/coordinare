"""Build actual pinned source identities for performer contract regressions."""
from __future__ import annotations

import copy
import hashlib
import shutil
import subprocess
from pathlib import Path

from coordinare_service_inference.environment.execution_source import SourceEntry, source_identity
from coordinare_service_inference.environment.models import EnvironmentManifest


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
    manifest = EnvironmentManifest.model_validate({**values['environment_manifest'], 'source_commit':commit,
        'inputs':{'tools.plan':hashlib.sha256(content).hexdigest()}})
    values.update(environment_manifest=manifest.model_dump(), environment_manifest_digest=manifest.digest(),
        environment_execution_source=source.model_dump())
    values['environment_assessment'].update(source_commit=commit, manifest_digest=manifest.digest(), inputs=dict(manifest.inputs))
    values['environment_assessment'] = current_review_fixture(manifest, values['environment_assessment'])
    return values
