"""Durable finite bootstrap budgets and rejected executable repair evidence."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from coordinare_service_inference.environment.models import (
    EnvironmentManifest,
    ExecutionCapabilities,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from coordinare.lib.redaction import redact_command_text, redact_state_value
from coordinare.services.environment_acceptance import (
    accepted_environment,
    execution_report,
    saved_execution_accepted,
)
from coordinare.services.environment_attempt import (
    MAX_ATTEMPT_SECONDS,
    AttemptBudget,
    EnvironmentDiscoveryError,
)

if TYPE_CHECKING:
    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.environment_discovery import DiscoveryLimits

JOURNAL_MAX_BYTES = 4 * 1024 * 1024
MAX_ARCHIVED_ATTEMPTS = 3
REPORT_MAX_BYTES = 256 * 1024
FEEDBACK_MAX_BYTES = 16 * 1024
OUTPUT_MAX_CHARS = 512


class RepairJournal(BaseModel):
    """Daemon-owned state; a pending repair survives snapshot roundtrips."""

    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    version: Literal[2] = 2
    source_commit: str = Field(pattern=r'^[0-9a-f]{40,64}$')
    base_slug: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,180}$')
    base_cache_dir: str
    origin_slug: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,180}$')
    generation_nonce: str | None = Field(default=None, pattern=r'^[0-9a-f]{12}$')
    started_at: float
    expires_at: float
    max_calls: int = Field(ge=1)
    max_reads: int = Field(ge=1)
    max_schema_repairs: int = Field(ge=0, le=2)
    max_coverage_revisions: int = Field(ge=0, le=2)
    max_execution_repairs: int = Field(ge=0, le=2)
    calls: int = Field(default=0, ge=0)
    reads: int = Field(default=0, ge=0)
    schema_repairs: int = Field(default=0, ge=0, le=2)
    coverage_revisions: int = Field(default=0, ge=0, le=2)
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
    if isinstance(data, dict) and data.get("version") == 1:
        if data.get("status") != "ready":
            _refuse("legacy environment run requires explicit restart after confirmed cleanup", code="legacy_attempt_requires_restart")
        # Only frozen generation metadata can be read from the old journal. No
        # pending legacy work can obtain renewed category allowances.
        data = dict(data)
        consumed, maximum = data.pop("repairs", None), data.pop("max_repairs", None)
        data["version"] = 2
        for name in ("schema_repairs", "coverage_revisions", "execution_repairs"):
            data[name], data["max_" + name] = consumed, maximum
    try:
        if len(json.dumps(cache.environment_attempt).encode()) > JOURNAL_MAX_BYTES:
            _refuse('environment repair journal exceeds its bound')
        journal = RepairJournal.model_validate(data)
    except (TypeError, ValueError, ValidationError):
        _refuse('environment repair journal is invalid')
    if not 0 < journal.expires_at - journal.started_at <= MAX_ATTEMPT_SECONDS:
        _refuse('environment repair deadline is invalid')
    return journal


def hold_uncertain_execution(cache: EnvCacheState) -> None:
    """Never admit another attempt while interrupted external work may survive."""
    journal = journal_for(cache)
    journal.status, journal.last_error = "held", "cleanup_unconfirmed"
    cache.cache_dir_ready = False
    _publish(cache, journal)


def restart_environment_attempt(cache: EnvCacheState, *, cleanup_confirmed: bool = False) -> None:
    """Explicit operator action, preserving bounded old proof in a new empty sibling."""
    data = cache.environment_attempt
    if data is None:
        return
    if not isinstance(data, dict) or data.get("version") not in (1, 2):
        _refuse("unknown environment journal cannot be reset without reconciliation")
    normalized = dict(data)
    if normalized["version"] == 1:
        used, maximum = normalized.pop("repairs", None), normalized.pop("max_repairs", None)
        normalized["version"] = 2
        for counter in ("schema_repairs", "coverage_revisions", "execution_repairs"):
            normalized[counter], normalized["max_" + counter] = used, maximum
    try:
        parsed = RepairJournal.model_validate(normalized)
    except ValidationError:
        _refuse("malformed environment journal requires reconciliation before reset")
    if not 0 < parsed.expires_at - parsed.started_at <= MAX_ATTEMPT_SECONDS:
        _refuse("invalid environment deadline requires reconciliation before reset")
    uncertain = (data.get("status") == "executing" or data.get("last_error") == "cleanup_unconfirmed"
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
    cache.environment_capabilities = None
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
    budget.calls, budget.reads = journal.calls, journal.reads
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
    previous = journal_for(cache) if cache.environment_attempt is not None else None
    if previous is not None and (previous.last_error == "cleanup_unconfirmed" or previous.status == "executing"):
        _refuse("environment repair requires confirmed cleanup of interrupted execution")
    seconds = min(limits.attempt_timeout_seconds, outer_seconds if outer_seconds > 0 else MAX_ATTEMPT_SECONDS,
                  MAX_ATTEMPT_SECONDS)
    if not isinstance(cache.bootstrap_commit_sha, str):
        _refuse("environment repair requires an immutable source commit")
    if previous is None or previous.source_commit != cache.bootstrap_commit_sha or previous.status == 'ready':
        journal = RepairJournal(source_commit=cache.bootstrap_commit_sha,
            base_slug=cache.sanitised_name, base_cache_dir=str(cache.cache_dir),
            origin_slug=previous.origin_slug if previous else cache.sanitised_name,
            started_at=now, expires_at=now+seconds, max_calls=limits.max_model_calls,
            max_reads=limits.max_total_reads, max_schema_repairs=limits.max_schema_repairs,
            max_coverage_revisions=limits.max_coverage_revisions, max_execution_repairs=limits.max_execution_repairs)
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


def save_attempt(cache: EnvCacheState, budget: AttemptBudget, *, executing: bool = False) -> None:
    """Save consumed reservations before external work; never move the original expiry."""
    journal = journal_for(cache)
    journal.diagnostics = budget.summary()
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


def _rejected_evidence(cache: EnvCacheState, data: dict[str, Any]) -> dict[str, Any]:
    manifest = accepted_environment(cache)
    if manifest is None:
        _refuse('execution repair requires independent acceptance')
    report = execution_report(json.dumps({key: data[key] for key in ('platform', 'manifest_digest', 'scope', 'results')
                                          if key in data}), cache, publish=False)
    target = ExecutionCapabilities.model_validate(cache.environment_capabilities)
    if report.platform != target.platform or (data.get('image_ref') is not None and data['image_ref'] != target.image_ref):
        _refuse('execution repair evidence runtime identity differs')
    if not any(result.required and result.status == 'fail' for result in report.results):
        _refuse('execution repair requires an actual failed action')
    known = {command.id for command in [*manifest.install, *manifest.checks]}
    known.update(command.id for service in manifest.services
                 for command in (service.init, service.start, service.health, service.stop) if command is not None)
    if any(result.id not in known or result.status == 'indeterminate' for result in report.results):
        _refuse('execution repair evidence is unknown or indeterminate')
    original_digest = hashlib.sha256(report.model_dump_json().encode()).hexdigest()
    for result in report.results:
        result.output = redact_command_text(result.output)[:OUTPUT_MAX_CHARS]
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
    if journal.status == "executing" or journal.last_error == "cleanup_unconfirmed" or (journal.status == "held" and journal.source_commit == commit):
        return "environment attempt is held; confirmed cleanup and a valid repair budget are required"
    return None
