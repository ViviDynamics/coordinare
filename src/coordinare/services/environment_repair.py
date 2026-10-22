"""Durable finite bootstrap budgets and rejected executable repair evidence."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from coordinare_service_inference.environment.evidence_safety import source_path
from coordinare_service_inference.environment.findings import (
    MAX_LEDGER_FINDINGS,
    Digest,
    FindingLedger,
    FindingResponse,
)
from coordinare_service_inference.environment.models import (
    EnvironmentManifest,
    ExecutionCapabilities,
)
from coordinare_service_inference.environment.scope import (
    ScopeContract,
    ScopeDefinition,
    SourceAssessmentRecord,
    validate_source_assessment,
)
from coordinare_service_inference.environment.scope_investigation import (
    ScopeInspectionBinding,  # noqa: TC002 - pydantic resolves journal field types at runtime
    ScopeInspectionResult,  # noqa: TC002 - pydantic resolves journal field types at runtime
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from coordinare.lib.redaction import redact_command_text, redact_state_value
from coordinare.services.environment_acceptance import (
    accepted_environment,
    execution_report,
    failed_installer_report,
    saved_execution_accepted,
)
from coordinare.services.environment_attempt import (
    MAX_ATTEMPT_SECONDS,
    MAX_SHARDED_ATTEMPT_SECONDS,
    REPAIR_LIMITS,
    AttemptBudget,
    EnvironmentDiscoveryError,
)
from coordinare.services.environment_obligations import (
    executed_manifests,
    preserve_execution_obligations,
    preserve_obligations,
)
from coordinare.services.environment_probes import ProbePlan, ProbeReport, bind_probe_report
from coordinare.services.environment_scope import preserve_scope
from coordinare.services.environment_scope_investigation import validate_investigation_history
from coordinare.services.environment_scope_reuse import SCOPE_POLICY_VERSION, reusable_scope

if TYPE_CHECKING:
    from coordinare_service_inference.environment.models import CheckResult

    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.environment_discovery import DiscoveryLimits

PRE_PROBE_JOURNAL_VERSION = 2
PRE_SOURCE_JOURNAL_VERSION = 3
JOURNAL_MAX_BYTES = 16 * 1024 * 1024
MAX_ARCHIVED_ATTEMPTS = 3
REPORT_MAX_BYTES = 256 * 1024
FEEDBACK_MAX_BYTES = 16 * 1024
OUTPUT_MAX_CHARS = 512


CURRENT_JOURNAL_VERSION = 9


class RepairJournal(BaseModel):
    """Daemon-owned state; a pending repair survives snapshot roundtrips."""

    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    version: Literal[9] = 9
    scope_author_protocol: Literal[4]
    scope_phase: Literal["initial", "investigation", "revision", "review", "accepted"] = "initial"
    scope_revision_base_digest: Digest | None = None
    scope_investigations: list[ScopeInspectionBinding] = Field(default_factory=list, max_length=256)
    scope_investigation_results: list[ScopeInspectionResult] = Field(default_factory=list, max_length=256)
    scope_inputs: dict[str, Digest] = Field(default_factory=dict)
    scope_responses: list[FindingResponse] = Field(default_factory=list, max_length=MAX_LEDGER_FINDINGS)
    scope_definition: ScopeDefinition | None = None
    scope_contract: ScopeContract | None = None
    scope_findings: FindingLedger | None = None
    scope_assessment: SourceAssessmentRecord | None = None
    # Sharded discovery progress; validated against the recomputed shard plan when resumed.
    scope_shards: dict[str, Any] | None = None
    # Divided executable progress; a part is reused only when its author's complete input digest matches.
    executable_parts: dict[str, Any] | None = None
    # An archived attempt's accepted scope offered for a fresh review, and the record once adopted.
    scope_reuse: dict[str, Any] | None = None
    scope_reused_from: dict[str, Any] | None = None
    scope_policy_version: str = SCOPE_POLICY_VERSION
    finding_ledger: FindingLedger | None = None
    coverage_manifest: EnvironmentManifest | None = None
    source_owner: str | None = None
    execution_source: dict[str, Any] | None = None
    probe_batches: int = Field(default=0, ge=0, le=2)
    probe_reports: list[dict[str, Any]] = Field(default_factory=list, max_length=2)
    probe_owner: str | None = None
    probe_plan: dict[str, Any] | None = None
    source_commit: str = Field(pattern=r'^[0-9a-f]{40,64}$')
    base_slug: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,180}$')
    base_cache_dir: str
    origin_slug: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,180}$')
    generation_nonce: str | None = Field(default=None, pattern=r'^[0-9a-f]{12}$')
    started_at: float
    expires_at: float
    max_calls: int = Field(ge=1)
    max_reads: int = Field(ge=1)
    max_schema_repairs: int = Field(ge=0, le=REPAIR_LIMITS["schema"])
    max_coverage_revisions: int = Field(ge=0, le=REPAIR_LIMITS["coverage"])
    max_execution_repairs: int = Field(ge=0, le=REPAIR_LIMITS["execution"])
    calls: int = Field(default=0, ge=0)
    reads: int = Field(default=0, ge=0)
    schema_repairs: int = Field(default=0, ge=0, le=REPAIR_LIMITS["schema"])
    coverage_revisions: int = Field(default=0, ge=0, le=REPAIR_LIMITS["coverage"])
    execution_repairs: int = Field(default=0, ge=0, le=2)
    generation: int = Field(default=0, ge=0, le=2)
    status: Literal['discovering', 'executing', 'repair_pending', 'held', 'ready'] = 'discovering'
    target: ExecutionCapabilities | None = None
    rejected: list[dict[str, Any]] = Field(default_factory=list, max_length=3)
    last_error: str | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)


def _refuse(message: str, *, code: str = 'execution_repair_held') -> NoReturn:
    raise EnvironmentDiscoveryError(message, code=code)


def journal_for(cache: EnvCacheState) -> RepairJournal:
    """Parse only bounded, supported journal state, never repair unknown snapshots."""
    data = cache.environment_attempt
    if isinstance(data, dict) and type(data.get('version')) is int and 1 <= data['version'] < CURRENT_JOURNAL_VERSION:
        # Earlier source protocols lack a typed source assessment; their reviews and
        # contracts are retained for audit but never resumed or upgraded to accepted.
        _refuse('legacy environment review requires explicit restart after confirmed cleanup', code='legacy_attempt_requires_restart')
    if isinstance(data, dict) and data.get('version') == CURRENT_JOURNAL_VERSION and not {
            'scope_author_protocol', 'scope_phase', 'scope_revision_base_digest',
            'scope_investigations', 'scope_investigation_results', 'scope_assessment'} <= data.keys():
        _refuse('scope protocol checkpoint is incomplete', code='scope_protocol_invalid')
    try:
        if len(json.dumps(cache.environment_attempt).encode()) > JOURNAL_MAX_BYTES:
            _refuse('environment repair journal exceeds its bound')
        journal = RepairJournal.model_validate(data)
    except (TypeError, ValueError, ValidationError):
        _refuse('environment repair journal is invalid')
    if not 0 < journal.expires_at - journal.started_at <= MAX_SHARDED_ATTEMPT_SECONDS:
        _refuse('environment repair deadline is invalid')
    if journal.finding_ledger is not None and journal.finding_ledger.source_commit != journal.source_commit:
        _refuse('finding ledger source differs from attempt', code='source_identity')
    if (journal.finding_ledger is not None and journal.finding_ledger.reviews and journal.coverage_manifest is None):
        _refuse('coverage findings require their established candidate', code='review_findings_invalid')
    if journal.coverage_manifest is not None and journal.coverage_manifest.source_commit != journal.source_commit:
        _refuse('established candidate source differs', code='source_identity')
    if journal.coverage_manifest is not None:
        preserve_execution_obligations(journal.coverage_manifest, journal.rejected)
    _validate_scope_checkpoint(journal)
    return journal


def _validate_scope_checkpoint(journal: RepairJournal) -> None:
    definition, contract, findings = journal.scope_definition, journal.scope_contract, journal.scope_findings
    phase, base = journal.scope_phase, journal.scope_revision_base_digest
    if ((phase == 'initial' and (definition is not None or contract is not None or base is not None))
            or (phase == 'review' and (definition is None or contract is not None or base is not None))
            or (phase in {'investigation', 'revision'} and (definition is None or contract is not None
                or base != definition.digest() or findings is None or not findings.open_ids()))
            or (phase == 'accepted' and (contract is None or base is not None))):
        _refuse('scope phase differs from its retained definition', code='scope_protocol_invalid')
    _validate_source_assessment(journal)
    validate_investigation_history(bindings=journal.scope_investigations, results=journal.scope_investigation_results,
        ledger=findings, definition=definition, inputs=journal.scope_inputs)
    if phase in {'revision', 'review', 'accepted'} and findings is not None:
        completed = {result.binding_id for result in journal.scope_investigation_results}
        if any(binding.id not in completed for binding in journal.scope_investigations):
            _refuse('scope revision precedes required investigation', code='scope_investigation_identity')
    try:
        for path, digest in journal.scope_inputs.items():
            source_path(path)
            if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
                _refuse('scope input history is invalid', code='scope_source_identity')
    except (ValueError, TypeError):
        _refuse('scope input history is invalid', code='scope_source_identity')
    if definition is not None and any(journal.scope_inputs.get(path) != digest for path, digest in definition.inputs.items()):
        _refuse('scope reads differ from their definition', code='scope_source_identity')
    if definition is not None and definition.source_commit != journal.source_commit:
        _refuse('scope source differs from attempt', code='scope_source_identity')
    if findings is not None and ((findings.reviews and definition is None) or findings.source_commit != journal.source_commit):
        _refuse('scope findings lack their source definition', code='scope_source_identity')
    if contract is not None and (definition is None or contract.definition != definition
            or findings is None or contract.finding_ledger != findings):
        _refuse('scope contract differs from its independent review', code='scope_source_identity')


def _validate_source_assessment(journal: RepairJournal) -> None:
    """The latest typed source assessment must reproduce the latest ledger review exactly."""
    findings, record, contract = journal.scope_findings, journal.scope_assessment, journal.scope_contract
    reviewed = findings is not None and bool(findings.reviews)
    if (record is None) == reviewed:
        _refuse('source assessment differs from its review history', code='scope_protocol_invalid')
    if record is None or findings is None:
        return
    definition = journal.scope_definition
    # Only a pending review of a newly revised definition follows an assessment of its predecessor.
    assessed = definition if definition is not None and record.scope_digest == definition.digest() else None
    if assessed is None and (journal.scope_phase != 'review' or definition is None
                             or record.inventory_digest != definition.inventory_digest):
        _refuse('source assessment differs from its reviewed definition', code='scope_protocol_invalid')
    try:
        validate_source_assessment(record, ledger=findings, definition=assessed)
    except ValueError:
        _refuse('source assessment differs from its review history', code='scope_protocol_invalid')
    if (record.review_sequence != len(findings.reviews) or record.source_commit != journal.source_commit
            or any(journal.scope_inputs.get(path) != digest for path, digest in record.reviewer_inputs.items())):
        _refuse('source assessment differs from its review history', code='scope_protocol_invalid')
    if journal.scope_phase == 'accepted' and (contract is None or not contract.current
                                              or contract.source_assessment != record):
        _refuse('accepted scope lacks its typed source assessment', code='scope_protocol_invalid')


def _save_scope_checkpoint(journal: RepairJournal, budget: AttemptBudget) -> None:

    if journal.scope_definition is not None:
        if budget.scope_definition is None:
            _refuse('established scope cannot be removed', code='scope_obligation_removed')
        preserve_scope(journal.scope_definition, budget.scope_definition)
    if any(budget.scope_inputs.get(path) != digest for path, digest in journal.scope_inputs.items()):
        _refuse('scope read history cannot be replaced', code='scope_source_identity')
    if (budget.scope_investigations[:len(journal.scope_investigations)] != journal.scope_investigations
            or budget.scope_investigation_results[:len(journal.scope_investigation_results)] != journal.scope_investigation_results):
        _refuse('investigation history cannot be removed or replaced', code='scope_investigation_identity')
    journal.scope_investigations = list(budget.scope_investigations)
    journal.scope_investigation_results = list(budget.scope_investigation_results)
    journal.scope_author_protocol = budget.scope_author_protocol
    journal.scope_phase = budget.scope_phase
    journal.scope_revision_base_digest = budget.scope_revision_base_digest
    journal.scope_inputs = dict(budget.scope_inputs)
    journal.scope_shards = json.loads(json.dumps(budget.scope_shards)) if budget.scope_shards is not None else None
    journal.executable_parts = (json.loads(json.dumps(budget.executable_parts))
                                if budget.executable_parts is not None else None)
    journal.scope_reuse = budget.scope_reuse
    journal.scope_reused_from = budget.scope_reused_from
    try:
        journal.scope_responses = [FindingResponse.model_validate(value) for value in budget.scope_responses]
    except ValidationError:
        _refuse('scope responses are invalid', code='scope_response_ids_invalid')
    if journal.scope_assessment is not None and (budget.scope_assessment is None
            or budget.scope_assessment.review_sequence < journal.scope_assessment.review_sequence):
        _refuse('source assessment history cannot regress', code='scope_protocol_invalid')
    old, new = journal.scope_findings, budget.scope_findings
    if old is not None and (new is None or new.records[:len(old.records)] != old.records
                           or new.reviews[:len(old.reviews)] != old.reviews):
        _refuse('scope finding history cannot be replaced', code='review_findings_invalid')
    try:
        journal.scope_definition = ScopeDefinition.model_validate(budget.scope_definition.model_dump(mode='json')) if budget.scope_definition is not None else None
        journal.scope_contract = ScopeContract.model_validate(budget.scope_contract.model_dump(mode='json')) if budget.scope_contract is not None else None
        journal.scope_findings = FindingLedger.model_validate(budget.scope_findings.model_dump(mode='json')) if budget.scope_findings is not None else None
        journal.scope_assessment = (SourceAssessmentRecord.model_validate(budget.scope_assessment.model_dump(mode='json'))
                                    if budget.scope_assessment is not None else None)
    except ValidationError:
        _refuse('scope checkpoint is invalid', code='scope_source_identity')
    _validate_scope_checkpoint(journal)


def hold_uncertain_execution(cache: EnvCacheState) -> None:
    """Never admit another attempt while interrupted external work may survive."""
    journal = journal_for(cache)
    journal.status, journal.last_error = "held", "cleanup_unconfirmed"
    cache.cache_dir_ready = False
    _publish(cache, journal)


def execution_source_cleanup_confirmed(cache: EnvCacheState) -> bool:
    """Reconcile recorded local ownership by absence, never delete a persisted path."""
    data = cache.environment_attempt
    journal_owner = data.get("source_owner") if isinstance(data, dict) else None
    for owner in (cache.environment_source_owner, journal_owner):
        if owner is None:
            continue
        if not isinstance(owner, str) or not Path(owner).is_absolute():
            return False
        try:
            Path(owner).lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return False
        return False
    return True


def restart_environment_attempt(cache: EnvCacheState, *, cleanup_confirmed: bool = False) -> None:
    """Explicit operator action, preserving bounded old proof in a new empty sibling."""
    data = cache.environment_attempt
    if (cache.environment_source_owner is not None or (isinstance(data, dict) and data.get("source_owner") is not None)) and (
        cleanup_confirmed is not True or not execution_source_cleanup_confirmed(cache)
    ):
        _refuse("source cleanup must be reconciled before restart", code="cleanup_unconfirmed")
    if data is None:
        cache.environment_source_owner = None
        return
    if not isinstance(data, dict) or data.get("version") not in range(1, CURRENT_JOURNAL_VERSION+1):
        _refuse("unknown environment journal cannot be reset without reconciliation")
    normalized = dict(data)
    if normalized["version"] == 1:
        used, maximum = normalized.pop("repairs", None), normalized.pop("max_repairs", None)
        normalized["version"] = 2
        for counter in ("schema_repairs", "coverage_revisions", "execution_repairs"):
            normalized[counter], normalized["max_" + counter] = used, maximum
    if data['version'] < CURRENT_JOURNAL_VERSION:
        if normalized.get('scope_author_protocol') not in (None, 2, 3, 4):
            _refuse('unknown legacy scope protocol requires reconciliation')
        # Validation for explicit archival only; the original data is retained.
        normalized['scope_author_protocol'] = 4
        # A legacy executable candidate is bound to a contract the current policy refuses;
        # it is archived unchanged and never revalidated as current evidence.
        normalized['coverage_manifest'] = None
    normalized['version'] = CURRENT_JOURNAL_VERSION
    normalized.setdefault('scope_author_protocol', 4)
    normalized.setdefault("finding_ledger", None)
    try:
        parsed = RepairJournal.model_validate(normalized)
    except ValidationError:
        _refuse("malformed environment journal requires reconciliation before reset")
    if not 0 < parsed.expires_at - parsed.started_at <= MAX_SHARDED_ATTEMPT_SECONDS:
        _refuse("invalid environment deadline requires reconciliation before reset")
    uncertain = (data.get("source_owner") is not None or data.get("probe_owner") is not None or data.get("probe_plan") is not None
        or data.get("status") == "executing" or data.get("last_error") == "cleanup_unconfirmed"
        or (data.get("target") is not None and data.get("status") not in {"ready", "repair_pending"}))
    if cache.bootstrap_in_flight or (uncertain and cleanup_confirmed is not True):
        _refuse("operator restart requires confirmed executor cleanup", code="cleanup_unconfirmed")
    archive = [*cache.environment_attempt_archive, data]
    if len(archive) > MAX_ARCHIVED_ATTEMPTS or len(json.dumps(archive).encode()) > JOURNAL_MAX_BYTES:
        _refuse("operator restart requires archiving prior environment evidence", code="attempt_archive_limit")
    # Copy through JSON so future mutations cannot rewrite the rejected audit record.
    name = cache.sanitised_name[:140] + "-reset-" + uuid.uuid4().hex[:12]
    new_path = cache.cache_dir.parent / name
    if new_path.exists() or new_path.is_symlink():
        _refuse("operator restart cache generation is not fresh")
    cache.environment_attempt_archive = json.loads(json.dumps(archive))
    cache.cache_dir, cache.sanitised_name = new_path, name
    cache.environment_attempt = None
    cache.environment_manifest = cache.environment_assessment = cache.environment_verification = None
    cache.environment_runtime_artifacts = None
    cache.environment_capabilities = None
    cache.environment_execution_source = None
    cache.environment_source_owner = None
    cache.rendered_activate_sh = cache.rendered_verify_sh = None
    cache.cache_dir_ready = False
    cache.provenance_digest = None
    cache.readme_sha = None


def _publish(cache: EnvCacheState, journal: RepairJournal) -> None:
    data = journal.model_dump(mode='json')
    if len(json.dumps(data).encode()) > JOURNAL_MAX_BYTES:
        _refuse('environment repair journal exceeds its bound')
    cache.environment_attempt = data


def _budget(journal: RepairJournal, now: float) -> AttemptBudget:
    if now < journal.started_at:
        _refuse('environment repair clock moved backwards')
    if now >= journal.expires_at:
        _refuse('environment bootstrap deadline exceeded', code='attempt_deadline')
    if journal.status == 'held':
        _refuse('environment executable repair is held or exhausted')
    budget = AttemptBudget(timeout_seconds=journal.expires_at-journal.started_at,
        started=time.monotonic()-(now-journal.started_at), max_calls=journal.max_calls,
        max_reads=journal.max_reads, max_schema_repairs=journal.max_schema_repairs,
        max_coverage_revisions=journal.max_coverage_revisions, max_execution_repairs=journal.max_execution_repairs)
    budget.scope_investigations = [item.model_copy(deep=True) for item in journal.scope_investigations]
    budget.scope_investigation_results = [item.model_copy(deep=True) for item in journal.scope_investigation_results]
    budget.scope_author_protocol = journal.scope_author_protocol
    budget.scope_phase = journal.scope_phase
    budget.scope_revision_base_digest = journal.scope_revision_base_digest
    budget.scope_inputs = dict(journal.scope_inputs)
    budget.scope_shards = json.loads(json.dumps(journal.scope_shards)) if journal.scope_shards is not None else None
    budget.executable_parts = (json.loads(json.dumps(journal.executable_parts))
                               if journal.executable_parts is not None else None)
    budget.scope_reuse = json.loads(json.dumps(journal.scope_reuse)) if journal.scope_reuse is not None else None
    budget.scope_reused_from = journal.scope_reused_from
    budget.scope_responses = [r.model_dump(mode="json") for r in journal.scope_responses]
    budget.scope_definition = journal.scope_definition.model_copy(deep=True) if journal.scope_definition is not None else None
    budget.scope_contract = journal.scope_contract.model_copy(deep=True) if journal.scope_contract is not None else None
    budget.scope_findings = journal.scope_findings.model_copy(deep=True) if journal.scope_findings is not None else None
    budget.scope_assessment = journal.scope_assessment.model_copy(deep=True) if journal.scope_assessment is not None else None
    budget.finding_ledger = journal.finding_ledger.model_copy(deep=True) if journal.finding_ledger is not None else None
    budget.coverage_manifest = journal.coverage_manifest.model_copy(deep=True) if journal.coverage_manifest is not None else None
    budget.executed_manifests = executed_manifests(journal.rejected, source_commit=journal.source_commit)
    budget.execution_review_evidence = [{"manifest_digest": entry["manifest_digest"],
        "manifest": entry["manifest"], "results": [{key: result.get(key) for key in
        ("id", "status", "required", "exit_code", "duration_seconds")} for result in entry["report"]["results"]]}
        for entry in journal.rejected]
    budget.calls, budget.reads = journal.calls, journal.reads
    budget.probe_batches = journal.probe_batches
    budget.probe_reports = list(journal.probe_reports)
    for name in ("schema_repairs", "coverage_revisions", "execution_repairs"):
        if getattr(journal, name) > getattr(journal, "max_" + name):
            _refuse(f"environment {name} limit exceeded after configuration change", code="attempt_" + name.split("_")[0] + "_limit")
    budget.schema_repairs = journal.schema_repairs
    budget.coverage_revisions = journal.coverage_revisions
    budget.execution_repairs = journal.execution_repairs
    return budget


def begin_attempt(cache: EnvCacheState, limits: DiscoveryLimits, *, outer_seconds: float,
                  now: float | None = None) -> AttemptBudget:
    """Resume an existing finite budget; a new source gets a separate logical attempt."""
    now = time.time() if now is None else now
    if cache.environment_source_owner is not None:
        _refuse("execution source requires confirmed cleanup", code="cleanup_unconfirmed")
    previous = journal_for(cache) if cache.environment_attempt is not None else None
    if previous is not None and (previous.last_error == "cleanup_unconfirmed" or previous.status == "executing" or previous.probe_owner is not None or previous.source_owner is not None):
        _refuse("environment repair requires confirmed cleanup of interrupted execution")
    ceiling = MAX_SHARDED_ATTEMPT_SECONDS if limits.shard_context_tokens else MAX_ATTEMPT_SECONDS
    seconds = min(limits.attempt_timeout_seconds, outer_seconds if outer_seconds > 0 else ceiling, ceiling)
    if not isinstance(cache.bootstrap_commit_sha, str):
        _refuse("environment repair requires an immutable source commit")
    if previous is None or previous.source_commit != cache.bootstrap_commit_sha or previous.status == 'ready':
        journal = RepairJournal(source_commit=cache.bootstrap_commit_sha, scope_author_protocol=4,
            base_slug=cache.sanitised_name, base_cache_dir=str(cache.cache_dir),
            origin_slug=previous.origin_slug if previous else cache.sanitised_name,
            started_at=now, expires_at=now+seconds, max_calls=limits.max_model_calls,
            max_reads=limits.max_total_reads, max_schema_repairs=limits.max_schema_repairs,
            max_coverage_revisions=limits.max_coverage_revisions, max_execution_repairs=limits.max_execution_repairs)
        if previous is None:
            # After an operator restart, an archived attempt's accepted scope is offered for a fresh review.
            journal.scope_reuse = reusable_scope(cache.environment_attempt_archive, cache.bootstrap_commit_sha)
    else:
        journal = previous
        journal.expires_at = min(journal.expires_at, journal.started_at+seconds)
        journal.max_calls = min(journal.max_calls, limits.max_model_calls)
        journal.max_reads = min(journal.max_reads, limits.max_total_reads)
        journal.max_schema_repairs = min(journal.max_schema_repairs, limits.max_schema_repairs)
        journal.max_coverage_revisions = min(journal.max_coverage_revisions, limits.max_coverage_revisions)
        journal.max_execution_repairs = min(journal.max_execution_repairs, limits.max_execution_repairs)
    budget = _budget(journal, now)
    _publish(cache, journal)
    return budget


def _validate_established_candidate(journal: RepairJournal, candidate: EnvironmentManifest | None) -> None:
    if journal.coverage_manifest is not None:
        if candidate is None:
            _refuse('established candidate cannot be removed', code='obligation_removed')
        preserve_obligations(candidate, journal.coverage_manifest)
        if any(candidate.inputs.get(path) != digest for path, digest in journal.coverage_manifest.inputs.items()):
            _refuse('established source evidence changed', code='source_identity')
    if candidate is not None and candidate.source_commit != journal.source_commit:
        _refuse('established candidate source changed', code='source_identity')
    if candidate is not None:
        preserve_execution_obligations(candidate, journal.rejected)


def save_attempt(cache: EnvCacheState, budget: AttemptBudget, *, executing: bool = False) -> None:
    """Save consumed reservations before external work; never move the original expiry."""
    journal = journal_for(cache)
    _save_scope_checkpoint(journal, budget)
    previous, current = journal.finding_ledger, budget.finding_ledger
    if previous is not None and (current is None
            or current.records[:len(previous.records)] != previous.records
            or current.reviews[:len(previous.reviews)] != previous.reviews):
        _refuse('finding ledger history cannot be removed or replaced', code='review_findings_invalid')
    if current is not None:
        try:
            current = FindingLedger.model_validate(current.model_dump(mode='json'))
        except ValidationError:
            _refuse('finding ledger checkpoint is invalid', code='review_findings_invalid')
        if current.source_commit != journal.source_commit:
            _refuse('finding source changed during attempt', code='source_identity')
    candidate = budget.coverage_manifest
    _validate_established_candidate(journal, candidate)
    if current is not None and current.reviews and candidate is None:
        _refuse('coverage findings require established candidate', code='review_findings_invalid')
    journal.coverage_manifest = candidate
    journal.finding_ledger = current
    if budget.error_code is not None:
        journal.status, journal.last_error = 'held', budget.error_code
    journal.diagnostics = budget.summary()
    journal.probe_batches = max(journal.probe_batches, budget.probe_batches)
    journal.calls = max(journal.calls, budget.calls)
    journal.reads = max(journal.reads, budget.reads)
    journal.schema_repairs = max(journal.schema_repairs, budget.schema_repairs)
    journal.coverage_revisions = max(journal.coverage_revisions, budget.coverage_revisions)
    journal.execution_repairs = max(journal.execution_repairs, budget.execution_repairs)
    if executing:
        capabilities = ExecutionCapabilities.model_validate(cache.environment_capabilities)
        if journal.target is not None and (
            journal.target.model_dump(exclude={'cache_mount_path'}) != capabilities.model_dump(exclude={'cache_mount_path'})
        ):
            _refuse('environment repair runtime identity changed')
        journal.target = capabilities
        journal.status = 'executing'
    _publish(cache, journal)


def reserve_probe(cache: EnvCacheState, plan: ProbePlan) -> None:
    """Claim an already-reserved batch before any external executor work."""
    journal = journal_for(cache)
    if (journal.probe_owner is not None or journal.status != "discovering"
            or journal.probe_batches != len(journal.probe_reports) + 1 or journal.source_commit != plan.source_commit):
        _refuse("environment probe overlaps or lacks a durable reservation", code="probe_reservation_invalid")
    journal.probe_owner = plan.digest()
    journal.probe_plan = plan.model_dump(mode="json")
    _publish(cache, journal)


def finish_probe(cache: EnvCacheState, report: ProbeReport) -> None:
    """Release ownership only after exact complete evidence and confirmed cleanup."""
    journal = journal_for(cache)
    if journal.probe_plan is None or journal.probe_owner != report.plan_digest:
        _refuse("environment probe ownership mismatch", code="probe_report_invalid")
    plan = ProbePlan.model_validate(journal.probe_plan)
    bound = bind_probe_report(plan, report)
    journal.probe_reports.append(bound.model_dump(mode="json"))
    journal.probe_owner = None
    journal.probe_plan = None
    _publish(cache, journal)


def _activation_failure(result: CheckResult, manifest: EnvironmentManifest) -> bool:
    """Recognize only the runner's bound missing-activation failure, never success."""
    return (bool(manifest.activation.path) and result.id == "activation" and result.required
        and result.status == "fail" and result.exit_code is None
        and result.manifest_digest == manifest.digest() and result.context == "")


def _rejected_evidence(cache: EnvCacheState, data: dict[str, Any]) -> dict[str, Any]:
    manifest = accepted_environment(cache)
    if manifest is None:
        _refuse('execution repair requires independent acceptance')
    evidence_text = json.dumps({key: data[key] for key in ('platform', 'manifest_digest', 'execution_source_digest', 'scope', 'results',
                                                                    'runtime_digest', 'runtime_phase', 'runtime_artifacts', 'runtime_cleanup')
                                          if key in data})
    report = (failed_installer_report(evidence_text, cache) if data.get('runtime_phase') == 'installer'
              else execution_report(evidence_text, cache, publish=False))
    target = ExecutionCapabilities.model_validate(cache.environment_capabilities)
    if report.platform != target.platform or (data.get('image_ref') is not None and data['image_ref'] != target.image_ref):
        _refuse('execution repair evidence runtime identity differs')
    if not any(result.required and result.status == 'fail' for result in report.results):
        _refuse('execution repair requires an actual failed action')
    known = {command.id for command in [*manifest.install, *manifest.checks]}
    known.update(command.id for service in manifest.services
                 for command in (service.init, service.start, service.health, service.stop) if command is not None)
    if any((result.id not in known and not _activation_failure(result, manifest))
           or result.status == 'indeterminate' for result in report.results):
        _refuse('execution repair evidence is unknown or indeterminate')
    original_digest = hashlib.sha256(report.model_dump_json().encode()).hexdigest()
    for result in report.results:
        result.output = redact_command_text(result.output)[-OUTPUT_MAX_CHARS:]
        result.output_truncated = True
    if len(report.model_dump_json().encode()) > REPORT_MAX_BYTES:
        _refuse('execution repair report exceeds its bound')
    return {'manifest_digest': manifest.digest(), 'manifest': manifest.model_dump(mode='json'),
            'assessment': cache.environment_assessment, 'report': report.model_dump(mode='json'),
            'original_report_digest': original_digest}


def record_execution_failure(cache: EnvCacheState, report: dict[str, Any], *, cleanup_confirmed: bool,
                             now: float | None = None) -> bool:
    """Only complete failed parent evidence with confirmed cleanup can request repair."""
    journal = journal_for(cache)
    cache.cache_dir_ready = False
    try:
        if journal.target is None or journal.target.model_dump(mode="json") != cache.environment_capabilities:
            _refuse("execution repair target identity changed")
        if journal.status != "executing":
            _refuse("executable repair has no new execution to reject")
        if cleanup_confirmed is not True:
            _refuse('executable repair cleanup was not confirmed', code='cleanup_unconfirmed')
        evidence = _rejected_evidence(cache, report)
        journal.rejected.append(evidence)
        budget = _budget(journal, time.time() if now is None else now)
        budget.reserve_repair("execution")
        journal.execution_repairs = budget.execution_repairs
        journal.status = 'repair_pending'
    except (KeyError, TypeError, ValueError, EnvironmentDiscoveryError) as exc:
        journal.status = 'held'
        journal.last_error = redact_state_value(
            exc.code if isinstance(exc, EnvironmentDiscoveryError) else 'invalid_execution_evidence')
        _publish(cache, journal)
        return False
    else:
        _publish(cache, journal)
        return True


def _generation(journal: RepairJournal) -> str:
    if not journal.generation:
        return journal.base_slug
    if journal.generation > len(journal.rejected):
        _refuse('environment repair generation lacks rejected evidence')
    digest = journal.rejected[journal.generation-1].get('manifest_digest', '')
    if re.fullmatch(r'[0-9a-f]{64}', digest) is None:
        _refuse('environment repair generation identity is invalid')
    if journal.generation_nonce is None:
        _refuse('environment repair generation nonce is missing')
    owner = hashlib.sha256(journal.origin_slug.encode()).hexdigest()[:12]
    return f'{journal.origin_slug[:120]}-{owner}-r{journal.generation}-{journal.generation_nonce}'


def restore_attempt_generation(cache: EnvCacheState, *, interrupted: bool = False) -> None:
    """Restore only a derived cache sibling under the configured cache parent."""
    journal = journal_for(cache)
    name = _generation(journal)
    if (Path(journal.base_cache_dir).parent != cache.cache_dir.parent
            or cache.sanitised_name not in {journal.origin_slug, journal.base_slug, name}):
        _refuse('environment repair generation differs from configured cache parent')
    cache.sanitised_name = name
    cache.cache_dir = (cache.cache_dir.parent / name) if journal.generation else Path(journal.base_cache_dir)
    if interrupted and journal.status == 'executing':
        journal.status = 'held'
        journal.last_error = 'cleanup_unconfirmed'
        _publish(cache, journal)
        cache.cache_dir_ready = False
        cache.last_bootstrap_succeeded = False
        cache.last_bootstrap_error = 'interrupted environment execution requires confirmed cleanup'


def prepare_repair(cache: EnvCacheState) -> tuple[EnvironmentManifest, dict[str, Any]]:
    """Move a pending repair to a never-used cache generation, retaining rejected proof."""
    journal = journal_for(cache)
    if journal.status != 'repair_pending' or not journal.rejected:
        _refuse('environment repair is not pending')
    if Path(journal.base_cache_dir).parent != cache.cache_dir.parent:
        _refuse('environment repair generation differs from configured cache parent')
    entry = journal.rejected[-1]
    previous = EnvironmentManifest.model_validate(entry['manifest'])
    journal.generation = len(journal.rejected)
    journal.generation_nonce = uuid.uuid4().hex[:12]
    journal.status = 'discovering'
    new_path = cache.cache_dir.parent / _generation(journal)
    if new_path.exists() or new_path.is_symlink():
        _refuse('environment repair generation is not fresh')
    _publish(cache, journal)
    # The live entry may already name the preceding repaired generation.
    if cache.sanitised_name != journal.base_slug:
        cache.sanitised_name = journal.base_slug
    restore_attempt_generation(cache)
    cache.environment_manifest = cache.environment_assessment = cache.environment_verification = None
    cache.environment_runtime_artifacts = None
    cache.rendered_activate_sh = cache.rendered_verify_sh = None
    cache.provenance_digest = None
    cache.cache_dir_ready = False
    failed = [result for result in entry['report']['results'] if result['status'] != 'pass']
    feedback = {'rejected_manifest_digest': previous.digest(), 'results': failed[:16]}
    if len(json.dumps(feedback).encode()) > FEEDBACK_MAX_BYTES:
        _refuse('environment repair feedback exceeds its bound')
    return previous, feedback


def mark_attempt_ready(cache: EnvCacheState, *, now: float | None = None) -> None:
    """A successful final readiness decision retains all earlier rejected evidence."""
    if cache.environment_attempt is not None:
        if not saved_execution_accepted(cache) or cache.provenance_digest is None:
            _refuse("environment readiness lacks required execution or frozen provenance")
        journal = journal_for(cache)
        _budget(journal, time.time() if now is None else now)
        journal.status = 'ready'
        _publish(cache, journal)


def remaining_seconds(cache: EnvCacheState, *, now: float | None = None) -> float:
    """Return the original deadline's remainder; unknown/held state cannot renew it."""
    journal = journal_for(cache)
    now = time.time() if now is None else now
    _budget(journal, now)
    return journal.expires_at-now


def repair_context(cache: EnvCacheState) -> tuple[EnvironmentManifest | None, dict[str, Any] | None]:
    """Return bounded feedback only after a fresh generation has been prepared."""
    journal = journal_for(cache)
    if not journal.generation or not journal.rejected:
        return None, None
    if journal.status != 'discovering':
        _refuse('environment repair requires a prepared fresh cache generation')
    entry = journal.rejected[-1]
    previous = EnvironmentManifest.model_validate(entry['manifest'])
    failed = [item for item in entry['report']['results'] if item['status'] != 'pass']
    return previous, {'rejected_manifest_digest': previous.digest(), 'results': failed[:16]}


def finish_attempt(cache: EnvCacheState, success: bool, evidence: dict[str, Any] | None,
                   *, cleanup_confirmed: bool) -> None:
    """Complete one execution without resetting its journal or rejected history."""
    if cache.environment_attempt is None:
        return
    if success:
        mark_attempt_ready(cache)
        return
    journal = journal_for(cache)
    if journal.status == 'executing':
        record_execution_failure(cache, evidence or {}, cleanup_confirmed=cleanup_confirmed)
    elif journal.status == 'discovering':
        journal.status = 'held'
        journal.last_error = 'discovery_refused'
        _publish(cache, journal)


def dispatch_refusal(cache: EnvCacheState, commit: str) -> str | None:
    """Held execution cannot burn another dispatch budget or bypass unknown cleanup."""
    if cache.environment_attempt is None:
        return None
    try:
        journal = journal_for(cache)
    except (ValueError, RuntimeError):
        return "environment attempt journal is invalid; dispatch held"
    if (journal.probe_owner is not None or journal.probe_plan is not None or journal.status == "executing"
            or journal.last_error == "cleanup_unconfirmed" or (journal.status == "held" and journal.source_commit == commit)):
        return "environment attempt is held; confirmed cleanup and a valid repair budget are required"
    return None


def reserve_execution_source(cache: EnvCacheState, owner: str) -> None:
    """Persist temporary source ownership before acquisition can create content."""
    if cache.environment_source_owner is not None:
        _refuse('execution source ownership requires confirmed cleanup', code='cleanup_unconfirmed')
    if cache.environment_attempt is not None:
        journal = journal_for(cache)
        if journal.source_owner is not None:
            _refuse('execution source ownership requires confirmed cleanup', code='cleanup_unconfirmed')
        journal.source_owner = owner
        _publish(cache, journal)
    cache.environment_source_owner = owner


def release_execution_source(cache: EnvCacheState, *, identity: dict[str, Any] | None = None) -> None:
    """Release only after the caller removed its owned temporary source directory."""
    owner = cache.environment_source_owner
    if owner is not None and (Path(owner).exists() or Path(owner).is_symlink()):
        _refuse('execution source cleanup unconfirmed', code='cleanup_unconfirmed')
    if cache.environment_attempt is not None:
        journal = journal_for(cache)
        if journal.source_owner is not None and (Path(journal.source_owner).exists() or Path(journal.source_owner).is_symlink()):
            _refuse('execution source cleanup unconfirmed', code='cleanup_unconfirmed')
        if identity is not None:
            if journal.execution_source is not None and journal.execution_source != identity:
                _refuse('execution source changed within the pinned attempt', code='execution_source_identity')
            journal.execution_source = identity
        journal.source_owner = None
        _publish(cache, journal)
    cache.environment_source_owner = None
