"""Debug-only resume of the validation harness from an earlier run's accepted contract (issue 376).

A resumed run starts at the installer with zero model calls, so installer, repair, clean-consumer and provenance
fixes can be proven quickly. It is never gate evidence: the saved contract was reviewed against another run's
runtime binding, and only that binding is moved here, without a fresh independent review.

Only these fields change between runs and are rebound: the manifest's runtime policy (it embeds the carrier image,
rebuilt from the current checkout) and capabilities digest, and the assessment's capabilities digest, manifest
digest, finding ledger digest and the last ledger review's manifest digest. The source commit, inventory, inputs,
reviewed scope, symphony and execution source must match, and anything else that differs refuses the resume.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity
from coordinare_service_inference.environment.findings import FindingLedger
from coordinare_service_inference.environment.models import (
    CoverageAssessment,
    EnvironmentManifest,
    ExecutionCapabilities,
    validate_current_coverage,
)
from coordinare_service_inference.environment.runtime import RuntimeSpec

from coordinare.models.env_cache import EnvCacheState
from coordinare.services.environment_acceptance import accepted_environment

_ATTEMPT = re.compile(r'attempt-(\d{3})')
# The manifest fields a rebind may change; every other field must be identical.
_MANIFEST_BINDING = frozenset({'runtime', 'capabilities_digest'})
_ASSESSMENT_BINDING = frozenset({'capabilities_digest', 'manifest_digest', 'finding_ledger', 'finding_ledger_digest'})


class ResumeRefusedError(ValueError):
    """A saved run cannot be resumed; the message names why."""


@dataclass(frozen=True)
class ResumeSource:
    """One saved attempt's accepted contract, validated with the current models."""

    source_dir: Path
    repo: str
    attempt: int
    attempt_dir: Path
    manifest: EnvironmentManifest
    assessment: CoverageAssessment
    execution_source: dict[str, Any]
    capabilities: dict[str, Any] | None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        message = f'{path.name} is unreadable: {type(exc).__name__}'
        raise ResumeRefusedError(message) from None


def _has_contract(directory: Path) -> bool:
    return (directory / 'accepted-manifest.json').is_file() and (directory / 'accepted-assessment.json').is_file()


def _select_attempt(repository: Path, attempt: int | None) -> tuple[int, Path]:
    if attempt is not None:
        if attempt < 0:
            message = 'attempt number must not be negative'
            raise ResumeRefusedError(message)
        directory = repository / f'attempt-{attempt:03}'
        if not _has_contract(directory):
            message = f'{directory.name} has no accepted manifest and assessment'
            raise ResumeRefusedError(message)
        return attempt, directory
    numbered = sorted((int(match.group(1)), path) for path in repository.iterdir()
                      if path.is_dir() and (match := _ATTEMPT.fullmatch(path.name)))
    candidates = [(number, path) for number, path in numbered if _has_contract(path)]
    if not candidates:
        message = 'source run has no attempt with an accepted manifest and assessment'
        raise ResumeRefusedError(message)
    return candidates[-1]


def load_source(source_dir: Path, repo: str, attempt: int | None = None) -> ResumeSource:
    """Load one finished run's accepted contract; an unfinished run (no result.json) is refused, never read."""
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', repo) or repo in {'.', '..'}:
        message = 'invalid repository name'
        raise ResumeRefusedError(message)
    root = Path(source_dir).resolve(strict=False)
    repository = root / repo
    # result.json is written last, so its absence means the run may still be in progress.
    if not (repository / 'result.json').is_file():
        message = 'source run has no result.json; an unfinished run is never resumed from'
        raise ResumeRefusedError(message)
    number, directory = _select_attempt(repository, attempt)
    try:
        manifest = EnvironmentManifest.model_validate(_read_json(directory / 'accepted-manifest.json'))
        assessment = CoverageAssessment.model_validate(_read_json(directory / 'accepted-assessment.json'))
    except ResumeRefusedError:
        raise
    except ValueError as exc:
        message = f'saved contract fails the current models: {type(exc).__name__}'
        raise ResumeRefusedError(message) from None
    if assessment.verdict != 'accept':
        message = 'saved assessment is not an acceptance'
        raise ResumeRefusedError(message)
    try:
        validate_current_coverage(manifest, assessment)
    except ValueError:
        message = 'saved assessment does not accept the saved manifest under the current policy'
        raise ResumeRefusedError(message) from None
    source_path = directory / 'execution-source.json'
    if not source_path.is_file():
        message = 'saved attempt has no execution-source.json'
        raise ResumeRefusedError(message)
    execution_source = _read_json(source_path)
    try:
        identity = ExecutionSourceIdentity.model_validate(execution_source)
    except ValueError:
        message = 'saved execution source identity is invalid'
        raise ResumeRefusedError(message) from None
    if identity.source_commit != manifest.source_commit:
        message = 'saved execution source was built from another commit'
        raise ResumeRefusedError(message)
    capabilities_path = directory / 'execution-capabilities.json'
    capabilities = _read_json(capabilities_path) if capabilities_path.is_file() else None
    return ResumeSource(source_dir=root, repo=repo, attempt=number, attempt_dir=directory, manifest=manifest,
                        assessment=assessment, execution_source=identity.model_dump(mode='json'),
                        capabilities=capabilities if isinstance(capabilities, dict) else None)


def archived_attempt(source: ResumeSource) -> dict[str, Any] | None:
    """The source run's journal, archived as --reuse-scope-from does, so a repair generation can offer its scope."""
    path = source.source_dir / source.repo / 'environment-attempt.json'
    data = _read_json(path) if path.is_file() else None
    return data if isinstance(data, dict) else None


def _refuse(message: str) -> ResumeRefusedError:
    return ResumeRefusedError('rebind refused: ' + message)


def _rebound_manifest(manifest: EnvironmentManifest, capabilities: ExecutionCapabilities) -> EnvironmentManifest:
    data = manifest.model_dump(mode='json')
    if (manifest.runtime is None) != (capabilities.runtime is None):
        raise _refuse('the saved contract and the current target differ in whether a private runtime is used')
    if manifest.runtime is not None and capabilities.runtime is not None:
        if manifest.runtime.policy.identity.guest_platform != capabilities.runtime.identity.guest_platform:
            raise _refuse('the current target selected a different guest platform')
        # Built as discovery builds it: the target's selected policy with the reviewed endpoints.
        data['runtime'] = RuntimeSpec(schema_version=1, policy=capabilities.runtime,
                                      endpoints=list(manifest.runtime.endpoints)).model_dump(mode='json')
    data['capabilities_digest'] = capabilities.digest()
    try:
        rebound = EnvironmentManifest.model_validate(data)
    except ValueError:
        raise _refuse('the rebound manifest fails the current model') from None
    if (rebound.model_dump(mode='json', exclude=set(_MANIFEST_BINDING))
            != manifest.model_dump(mode='json', exclude=set(_MANIFEST_BINDING))):
        raise _refuse('a field other than the runtime binding changed')
    if manifest.runtime is not None and (rebound.runtime is None or rebound.runtime.endpoints != manifest.runtime.endpoints):
        raise _refuse('the reviewed runtime endpoints changed')
    return rebound


def _rebound_ledger(ledger: FindingLedger | None, old_digest: str, new_digest: str) -> FindingLedger:
    if ledger is None or not ledger.reviews:
        raise _refuse('the saved assessment has no finding ledger')
    last = ledger.reviews[-1]
    if last.manifest_digest != old_digest or last.added_ids:
        raise _refuse('the saved ledger does not close on the saved manifest')
    data = ledger.model_dump(mode='json')
    data['reviews'][-1]['manifest_digest'] = new_digest
    try:
        return FindingLedger.model_validate(data)
    except ValueError:
        raise _refuse('the rebound finding ledger fails the current model') from None


def rebind(manifest: EnvironmentManifest, assessment: CoverageAssessment,
           capabilities: ExecutionCapabilities) -> tuple[EnvironmentManifest, CoverageAssessment, list[str]]:
    """Move an accepted contract's runtime binding to ``capabilities``; pure, and nothing else may change.

    Returns the rebound manifest and assessment, and the binding field names whose values changed. Raises
    ResumeRefusedError (a ValueError) when the saved contract is not accepted, or when the result is not.
    """
    try:
        validate_current_coverage(manifest, assessment)
    except ValueError:
        raise _refuse('the saved assessment does not accept the saved manifest') from None
    rebound = _rebound_manifest(manifest, capabilities)
    ledger = _rebound_ledger(assessment.finding_ledger, manifest.digest(), rebound.digest())
    data = assessment.model_dump(mode='json')
    data.update(capabilities_digest=capabilities.digest(), manifest_digest=rebound.digest(),
                finding_ledger=ledger.model_dump(mode='json'), finding_ledger_digest=ledger.digest())
    try:
        review = CoverageAssessment.model_validate(data)
    except ValueError:
        raise _refuse('the rebound assessment fails the current model') from None
    if (review.model_dump(mode='json', exclude=set(_ASSESSMENT_BINDING))
            != assessment.model_dump(mode='json', exclude=set(_ASSESSMENT_BINDING))):
        raise _refuse('an assessment field other than the runtime binding changed')
    old_ledger = assessment.finding_ledger.model_dump(mode='json') if assessment.finding_ledger is not None else {}
    new_ledger = ledger.model_dump(mode='json')
    old_ledger['reviews'][-1].pop('manifest_digest')
    new_ledger['reviews'][-1].pop('manifest_digest')
    if old_ledger != new_ledger:
        raise _refuse('a finding ledger field other than the last review binding changed')
    _require_accepted(rebound, review, capabilities)
    changed = [name for name, before, after in (
        ('manifest.runtime.policy', manifest.runtime.policy if manifest.runtime else None,
         rebound.runtime.policy if rebound.runtime else None),
        ('manifest.capabilities_digest', manifest.capabilities_digest, rebound.capabilities_digest),
        ('assessment.capabilities_digest', assessment.capabilities_digest, review.capabilities_digest),
        ('assessment.manifest_digest', assessment.manifest_digest, review.manifest_digest),
        ('assessment.finding_ledger.reviews[-1].manifest_digest', manifest.digest(), rebound.digest()),
        ('assessment.finding_ledger_digest', assessment.finding_ledger_digest, review.finding_ledger_digest),
    ) if before != after]
    return rebound, review, changed


def _require_accepted(manifest: EnvironmentManifest, assessment: CoverageAssessment,
                      capabilities: ExecutionCapabilities) -> None:
    """The same acceptance check execution applies, on a throwaway cache bound to the current target."""
    cache = EnvCacheState(symphony_name=manifest.symphony_name, sanitised_name='resume-check',
                          cache_dir=Path('/nonexistent/resume-check'), bootstrap_commit_sha=manifest.source_commit,
                          environment_manifest=manifest.model_dump(mode='json'),
                          environment_assessment=assessment.model_dump(mode='json'),
                          environment_capabilities=capabilities.model_dump(mode='json'))
    if accepted_environment(cache) is None or manifest.capabilities_digest != capabilities.digest():
        raise _refuse('the rebound contract is not accepted')
    if (manifest.runtime.policy if manifest.runtime is not None else None) != capabilities.runtime:
        raise _refuse('the rebound runtime policy differs from the current target')


def _capabilities_digest(data: dict[str, Any] | None) -> str | None:
    try:
        return ExecutionCapabilities.model_validate(data).digest() if data is not None else None
    except ValueError:
        return None


def resumed_from_record(source: ResumeSource, *, manifest: EnvironmentManifest, assessment: CoverageAssessment,
                        capabilities: dict[str, Any] | None, changed: list[str]) -> dict[str, Any]:
    """What result.json records about the resume: the source, both identities and what was rebound."""
    return {
        'source_dir': str(source.source_dir),
        'attempt': source.attempt,
        'source_commit': source.manifest.source_commit,
        'inventory_digest': source.manifest.inventory_digest,
        'execution_source_digest': ExecutionSourceIdentity.model_validate(source.execution_source).digest(),
        'original': {
            'manifest_digest': source.manifest.digest(),
            'assessment_manifest_digest': source.assessment.manifest_digest,
            'capabilities_digest': source.manifest.capabilities_digest,
            'capabilities_record_digest': _capabilities_digest(source.capabilities),
        },
        'rebound': {
            'manifest_digest': manifest.digest(),
            'assessment_manifest_digest': assessment.manifest_digest,
            'capabilities_digest': manifest.capabilities_digest,
            'capabilities_record_digest': _capabilities_digest(capabilities),
        },
        'changed_binding_fields': list(changed),
    }
