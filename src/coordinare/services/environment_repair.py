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

from coordinare.lib.redaction import redact_command_text
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
REPORT_MAX_BYTES = 256 * 1024
FEEDBACK_MAX_BYTES = 16 * 1024
OUTPUT_MAX_CHARS = 512


class RepairJournal(BaseModel):
    """Daemon-owned state; a pending repair survives snapshot roundtrips."""

    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    version: Literal[1] = 1
    source_commit: str = Field(pattern=r'^[0-9a-f]{40,64}$')
    base_slug: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,180}$')
    base_cache_dir: str
    origin_slug: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,180}$')
    generation_nonce: str | None = Field(default=None, pattern=r'^[0-9a-f]{12}$')
    started_at: float
    expires_at: float
    max_calls: int = Field(ge=1)
    max_reads: int = Field(ge=1)
    max_repairs: int = Field(ge=0, le=2)
    calls: int = Field(default=0, ge=0)
    reads: int = Field(default=0, ge=0)
    repairs: int = Field(default=0, ge=0, le=2)
    generation: int = Field(default=0, ge=0, le=2)
    status: Literal['discovering', 'executing', 'repair_pending', 'held', 'ready'] = 'discovering'
    target: ExecutionCapabilities | None = None
    rejected: list[dict[str, Any]] = Field(default_factory=list, max_length=3)
    last_error: str | None = None


def _refuse(message: str, *, code: str = 'execution_repair_held') -> NoReturn:
    raise EnvironmentDiscoveryError(message, code=code)


def journal_for(cache: EnvCacheState) -> RepairJournal:
    """Parse only bounded, supported journal state, never repair unknown snapshots."""
    try:
        if len(json.dumps(cache.environment_attempt).encode()) > JOURNAL_MAX_BYTES:
            _refuse('environment repair journal exceeds its bound')
        journal = RepairJournal.model_validate(cache.environment_attempt)
    except (TypeError, ValueError, ValidationError):
        _refuse('environment repair journal is invalid')
    if not 0 < journal.expires_at - journal.started_at <= MAX_ATTEMPT_SECONDS:
        _refuse('environment repair deadline is invalid')
    return journal


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
        max_reads=journal.max_reads, max_repairs=journal.max_repairs)
    budget.calls, budget.reads, budget.repairs = journal.calls, journal.reads, journal.repairs
    return budget


def begin_attempt(cache: EnvCacheState, limits: DiscoveryLimits, *, outer_seconds: float,
                  now: float | None = None) -> AttemptBudget:
    """Resume an existing finite budget; a new source gets a separate logical attempt."""
    now = time.time() if now is None else now
    previous = journal_for(cache) if cache.environment_attempt is not None else None
    if previous is not None and previous.last_error == "cleanup_unconfirmed":
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
            max_reads=limits.max_total_reads, max_repairs=limits.max_repairs)
    else:
        journal = previous
        journal.expires_at = min(journal.expires_at, journal.started_at+seconds)
        journal.max_calls = min(journal.max_calls, limits.max_model_calls)
        journal.max_reads = min(journal.max_reads, limits.max_total_reads)
        journal.max_repairs = min(journal.max_repairs, limits.max_repairs)
    budget = _budget(journal, now)
    _publish(cache, journal)
    return budget


def save_attempt(cache: EnvCacheState, budget: AttemptBudget, *, executing: bool = False) -> None:
    """Save consumed reservations before external work; never move the original expiry."""
    journal = journal_for(cache)
    journal.calls = max(journal.calls, budget.calls)
    journal.reads = max(journal.reads, budget.reads)
    journal.repairs = max(journal.repairs, budget.repairs)
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
        budget.reserve_repair()
        journal.repairs = budget.repairs
        journal.status = 'repair_pending'
    except (KeyError, TypeError, ValueError, EnvironmentDiscoveryError) as exc:
        journal.status = 'held'
        journal.last_error = exc.code if isinstance(exc, EnvironmentDiscoveryError) else 'invalid_execution_evidence'
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
    if journal.last_error == "cleanup_unconfirmed" or (journal.status == "held" and journal.source_commit == commit):
        return "environment attempt is held; confirmed cleanup and a valid repair budget are required"
    return None
