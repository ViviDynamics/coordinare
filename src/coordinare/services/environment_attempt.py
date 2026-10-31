"""Trusted environment attempt failures, shared resource budgets and safe diagnostics."""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from coordinare_service_inference.environment.findings import finding_summary

from coordinare.services.model_output_cap import REPETITION_FINISH

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from coordinare_service_inference.environment.findings import FindingLedger
    from coordinare_service_inference.environment.models import EnvironmentManifest
    from coordinare_service_inference.environment.scope import (
        ScopeContract,
        ScopeDefinition,
        SourceAssessmentRecord,
    )
    from coordinare_service_inference.environment.scope_investigation import (
        ScopeInspectionBinding,
        ScopeInspectionResult,
    )

    from coordinare.services.environment_repair_triage import RepairTriage


MAX_ATTEMPT_SECONDS = 3600
# Maintainer rulings (issue 353): sharded discovery on a self-hosted model may run up to six hours (raised from four
# on 2026-10-29: scope re-review, review rounds, install, a repair and the offline consumer ran past four).
MAX_SHARDED_ATTEMPT_SECONDS = 6 * 3600
# One phase stays within four hours of the six (the config caps manifest_phase_timeout_seconds the same way).
MAX_PHASE_SECONDS = 4 * 3600
# Maintainer ruling 2026-10-31: four execution repairs (Dettmore e2e-33 held with every install but one passing).
REPAIR_LIMITS = {"schema": 8, "coverage": 8, "scope": 8, "execution": 4}
MAX_PROBE_BATCHES = 2
# Each execution repair relocates its cache and brings one more probe batch (see reserve_probe_batch).
PROBE_BATCH_CEILING = MAX_PROBE_BATCHES + REPAIR_LIMITS["execution"]
RepairKind = Literal["schema", "coverage", "scope", "execution"]
# "coverage" is the executable review's revision budget (its name predates the split); "scope" is the source scope's.
REPAIR_COUNTERS = {"schema": "schema_repairs", "coverage": "coverage_revisions", "scope": "scope_revisions",
                   "execution": "execution_repairs"}
HOLD_CODES = {"schema": "attempt_schema_limit", "coverage": "attempt_coverage_limit",
              "scope": "attempt_scope_revision_limit", "execution": "attempt_execution_limit"}


class EnvironmentDiscoveryError(RuntimeError):
    """Discovery cannot establish an accepted, evidence-bound environment."""

    def __init__(self, message: str, *, code: str = "environment_discovery_held", global_owned: bool = False,
                 detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        # Set when only the global author's records can fix it, even though the message names check ids.
        self.global_owned = global_owned
        # Daemon-composed text safe to journal as the hold's error_detail (never model or command output).
        self.detail = detail


CALL_STAT_KEYS = ('calls', 'seconds', 'input_tokens', 'output_tokens', 'cached_tokens', 'length_stops', 'repetition_stops')


@dataclass(frozen=True)
class DiscoveryChatReply:
    text: str
    usage: Mapping[str, int] | None = None
    finish_reason: str | None = None
    error_code: str | None = None
    wire_text: str | None = None
    transport_fallback: str | None = None


def failure_detail(exc: BaseException) -> str:
    """Exception class, the innermost frame, and the innermost coordinare frame that called into it (file, function,
    line). Names the producer without echoing its message."""
    frames = traceback.extract_tb(exc.__traceback__)
    def where(frame: traceback.FrameSummary) -> str:
        return f"{frame.filename.rsplit('/', 1)[-1]}:{frame.name}:{frame.lineno}"
    owned = [frame for frame in frames if "/src/coordinare/" in frame.filename]
    return (f"{type(exc).__name__} at {where(frames[-1]) if frames else 'none'}"
            f"; in {where(owned[-1]) if owned else 'none'}")


@dataclass
class AttemptBudget:
    """One monotonic budget across inspection, malformed proposals and fresh reviews."""

    timeout_seconds: float = 600
    max_calls: int = 128
    max_reads: int = 512
    max_schema_repairs: int = 2
    max_coverage_revisions: int = 2
    max_scope_revisions: int = 8
    max_execution_repairs: int = REPAIR_LIMITS["execution"]
    started: float = field(default_factory=time.monotonic)
    calls: int = 0
    calls_completed: int = 0
    reads: int = 0
    # The daemon's own evidence verification (binding and delivering mandatory inputs), counted apart from the model's
    # capped reads. Maintainer ruling 2026-10-30: max_reads limits what the model asks for; verification stays
    # independent per role but is not charged to it (Dettmore e2e-22: 8,100 of 8,188 reads were verification).
    verification_reads: int = 0
    probe_batches: int = 0
    probe_reports: list[dict[str, Any]] = field(default_factory=list, repr=False)
    probe_abandonments: list[dict[str, Any]] = field(default_factory=list, repr=False)
    schema_repairs: int = 0
    coverage_revisions: int = 0
    scope_revisions: int = 0
    execution_repairs: int = 0
    review_context_bytes: int = 0
    mandatory_inputs_required: int = 0
    mandatory_inputs_delivered: int = 0
    inspected_bytes: int = 0
    context_bytes: int = 0
    expected_envelope: Literal["source_tool", "probe", "manifest", "manifest_delta", "assessment", "scope", "scope_assessment",
                               "scope_revision", "shard_summary", "listing_triage", "scope_consolidation", "shard_checks"] = "manifest"
    stage: str = "inventory"
    outcome: str = "held"
    error_code: str | None = None
    # The class and innermost frame behind an invalid_contract hold: never the message, which can carry model text.
    error_detail: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    # The pre-discovery guest decision: required platforms, triggering paths, and the guest chosen for this environment.
    guest: dict[str, Any] | None = None
    finish_reasons: dict[str, int] = field(default_factory=dict)
    transport_fallbacks: int = 0
    # Diagnostics only: each shard agent has its own correction allowance.
    shard_agents: int = 0
    shard_schema_repairs: int = 0
    metadata_available: bool = False
    validation_codes: list[str] = field(default_factory=list)
    finding_ledger: FindingLedger | None = field(default=None, repr=False)
    scope_investigation_results: list[ScopeInspectionResult] = field(default_factory=list, repr=False)
    scope_investigations: list[ScopeInspectionBinding] = field(default_factory=list, repr=False)
    scope_inputs: dict[str, str] = field(default_factory=dict, repr=False)
    scope_responses: list[dict[str, Any]] = field(default_factory=list, repr=False)
    scope_author_protocol: Literal[4] = 4
    scope_phase: Literal["initial", "investigation", "revision", "review", "accepted"] = "initial"
    scope_revision_base_digest: str | None = None
    # Required criteria an executable review asked to release as a per-run network fetch, pending the scope review.
    release_requests: list[str] = field(default_factory=list, repr=False)
    scope_definition: ScopeDefinition | None = field(default=None, repr=False)
    # Sharded discovery: plan digest, completed shard summaries and reviewed shard criteria.
    scope_shards: dict[str, Any] | None = field(default=None, repr=False)
    # Per-phase call cost: calls, seconds, input/output/cached tokens and length stops.
    call_stats: dict[str, dict[str, float]] = field(default_factory=dict, repr=False)
    last_finish_reason: str | None = field(default=None, repr=False)
    # An earlier attempt's accepted scope offered for a fresh review, and the record of having adopted one.
    scope_reuse: dict[str, Any] | None = field(default=None, repr=False)
    scope_reused_from: dict[str, Any] | None = field(default=None, repr=False)
    # Divided executable construction: completed global and check-group parts, keyed by what each author was given.
    executable_parts: dict[str, Any] | None = field(default=None, repr=False)
    scope_contract: ScopeContract | None = field(default=None, repr=False)
    scope_findings: FindingLedger | None = field(default=None, repr=False)
    scope_assessment: SourceAssessmentRecord | None = field(default=None, repr=False)
    coverage_manifest: EnvironmentManifest | None = field(default=None, repr=False)
    executed_manifests: list[EnvironmentManifest] = field(default_factory=list, repr=False)
    execution_review_evidence: list[dict[str, Any]] = field(default_factory=list, repr=False)
    # The failed execution's triage while a repair is pending; its closure is the only part a repair may change.
    repair_triage: RepairTriage | None = field(default=None, repr=False)
    # Whether this attempt authors a delta, and whether a delta already fell back to the full repair (persisted).
    repair_delta: bool = field(default=False, repr=False)
    delta_fallback: bool = False
    checkpoint: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (not 0 < self.timeout_seconds <= MAX_SHARDED_ATTEMPT_SECONDS or self.max_calls < 1 or self.max_reads < 1
                or any(not 0 <= getattr(self, "max_" + counter) <= REPAIR_LIMITS[kind] for kind, counter in REPAIR_COUNTERS.items())):
            message = "environment discovery budget limits are invalid"
            raise ValueError(message)

    @property
    def deadline(self) -> float:
        return self.started + self.timeout_seconds

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            message = "environment discovery budget deadline exceeded"
            raise EnvironmentDiscoveryError(message, code="attempt_deadline")

    def reserve_call(self) -> None:
        self.check()
        if self.calls >= self.max_calls:
            message = "environment discovery budget model call limit exceeded"
            raise EnvironmentDiscoveryError(message, code="attempt_call_limit")
        self.calls += 1

    def reserve_probe_batch(self) -> None:
        self.check()
        # An execution repair relocates its cache and must observe it with a fresh probe batch, so each repair
        # brings one: with a fixed two, v40's second permitted execution repair could never start.
        if self.probe_batches >= MAX_PROBE_BATCHES + self.execution_repairs:
            message = "environment probe batch limit exceeded"
            raise EnvironmentDiscoveryError(message, code="attempt_probe_limit")
        self.probe_batches += 1

    def reserve_reads(self, count: int, *, verification: bool = False) -> None:
        self.check()
        if verification:
            if type(count) is not int or count < 1:
                message = "environment discovery verification read count is invalid"
                raise EnvironmentDiscoveryError(message, code="attempt_read_limit")
            self.verification_reads += count
            return
        if type(count) is not int or count < 1 or self.reads + count > self.max_reads:
            message = "environment discovery budget file read limit exceeded"
            raise EnvironmentDiscoveryError(message, code="attempt_read_limit")
        self.reads += count

    @property
    def repairs(self) -> int:
        """Aggregate diagnostics only; authorization uses category-specific counters."""
        return self.schema_repairs + self.coverage_revisions + self.scope_revisions + self.execution_repairs

    def reserve_repair(self, kind: RepairKind) -> None:
        self.check()
        counter = REPAIR_COUNTERS[kind]
        if getattr(self, counter) >= getattr(self, "max_" + counter):
            message = f"environment discovery {kind} repair limit exceeded"
            raise EnvironmentDiscoveryError(message, code=HOLD_CODES[kind])
        if kind == "schema":
            self.schema_repairs += 1
        elif kind == "coverage":
            self.coverage_revisions += 1
        elif kind == "scope":
            self.scope_revisions += 1
        else:
            self.execution_repairs += 1

    async def flush(self) -> None:
        """Persist reservations before calling a model, source reader or executor."""
        self.check()
        if self.checkpoint is not None:
            await self.checkpoint()
        self.check()

    def record_reply(self, reply: str | DiscoveryChatReply, *, phase: str | None = None,
                     seconds: float | None = None) -> str:
        self.calls_completed += 1
        self.last_finish_reason = reply.finish_reason if isinstance(reply, DiscoveryChatReply) else None
        if phase is not None:
            self._record_call(phase, reply, seconds)
        if isinstance(reply, str):
            return reply
        if not isinstance(reply, DiscoveryChatReply):
            message = "model reply has unsupported metadata"
            raise EnvironmentDiscoveryError(message, code="model_reply_invalid")
        self.transport_fallbacks += reply.transport_fallback is not None
        if reply.error_code is not None:
            message = "environment model provider rejected or failed the request"
            raise EnvironmentDiscoveryError(message, code="provider_request_failed")
        self.metadata_available |= reply.usage is not None or reply.finish_reason is not None
        for key, value in (reply.usage or {}).items():
            if (key in {"input", "output", "input_tokens", "output_tokens", "cache_read", "cache_write"}
                    and type(value) is int and 0 <= value <= 10**12):
                self.usage[key] = self.usage.get(key, 0) + value
        if reply.finish_reason in {"stop", "length", "tool_calls", "content_filter", "function_call", REPETITION_FINISH}:
            self.finish_reasons[reply.finish_reason] = self.finish_reasons.get(reply.finish_reason, 0) + 1
        return reply.text

    def _record_call(self, phase: str, reply: str | DiscoveryChatReply, seconds: float | None) -> None:
        """Per-phase cost: where an attempt's time and tokens go, from trusted counters only."""
        stats = self.call_stats.setdefault(phase[:64], dict.fromkeys(CALL_STAT_KEYS, 0))
        stats['calls'] += 1
        stats['seconds'] = round(stats['seconds'] + max(0.0, seconds or 0.0), 3)
        if isinstance(reply, DiscoveryChatReply):
            usage = reply.usage or {}
            for key, names in (('input_tokens', ('input', 'input_tokens')), ('output_tokens', ('output', 'output_tokens')),
                               ('cached_tokens', ('cache_read',))):
                stats[key] += sum(value for name in names if type(value := usage.get(name)) is int and 0 <= value <= 10**12)
            stats['length_stops'] += reply.finish_reason == 'length'
            stats['repetition_stops'] = stats.get('repetition_stops', 0) + (reply.finish_reason == REPETITION_FINISH)

    def summary(self) -> dict[str, Any]:
        """Only trusted stages/codes, counters and allowlisted provider metadata escape."""
        return {**finding_summary(self.finding_ledger),
                **{"scope_" + key: value for key, value in finding_summary(self.scope_findings).items()},
                "scope_criterion_count": len(self.scope_definition.criteria) if self.scope_definition is not None else 0,
                "scope_review_count": len(self.scope_findings.reviews) if self.scope_findings is not None else 0,
                "scope_contract_digest": self.scope_contract.digest() if self.scope_contract is not None else None, "stage": self.stage, "outcome": self.outcome, "error_code": self.error_code, "error_detail": self.error_detail, "guest": self.guest,
                "elapsed_seconds": round(time.monotonic() - self.started, 3),
                "probe_batches": self.probe_batches, "abandoned_probe_batches": len(self.probe_abandonments),
                "max_probe_batches": MAX_PROBE_BATCHES, "calls": self.calls, "calls_completed": self.calls_completed, "reads": self.reads,
                "verification_reads": self.verification_reads,
                "repairs": self.repairs, **{name: getattr(self, name) for counter in REPAIR_COUNTERS.values()
                    for name in (counter, "max_" + counter)}, "inspected_bytes": self.inspected_bytes,
                "context_bytes": self.context_bytes, "review_context_bytes": self.review_context_bytes,
                "mandatory_inputs_required": self.mandatory_inputs_required,
                "mandatory_inputs_delivered": self.mandatory_inputs_delivered,
                "remaining_seconds": round(max(0, self.deadline - time.monotonic()), 3), "usage": dict(self.usage),
                "finish_reasons": dict(self.finish_reasons), "metadata_available": self.metadata_available,
                "transport_fallbacks": self.transport_fallbacks,
                "shard_agents": self.shard_agents, "shard_schema_repairs": self.shard_schema_repairs,
                "validation_codes": list(self.validation_codes),
                "call_stats": {phase: dict(stats) for phase, stats in self.call_stats.items()},
                "scope_reused_from": self.scope_reused_from}
