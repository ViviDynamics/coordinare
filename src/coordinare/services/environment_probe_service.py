"""Shared durable ownership and independent evidence for daemon/harness probes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from coordinare.lib.redaction import redact_command_text
from coordinare.services.environment_evidence import prepare_mandatory_evidence
from coordinare.services.environment_inspection import SourceInspection
from coordinare.services.environment_probe_executor import execute_environment_probe
from coordinare.services.environment_probes import ProbePlan, ProbeReport, validate_probe_report
from coordinare.services.environment_repair import finish_probe, reserve_probe

if TYPE_CHECKING:
    from collections.abc import Mapping

    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.environment_attempt import AttemptBudget
    from coordinare.services.environment_discovery import DiscoveryLimits, Fetch


@dataclass(frozen=True)
class ProbeServiceContext:
    cache: EnvCacheState
    state: Mapping[str, Any]
    fetch: Fetch
    budget: AttemptBudget
    limits: DiscoveryLimits


async def run_owned_probe(plan: ProbePlan, *, context: ProbeServiceContext) -> ProbeReport:
    cache, state, fetch, budget, limits = context.cache, context.state, context.fetch, context.budget, context.limits
    reserve_probe(cache, plan)
    await budget.flush()
    authorize = state.get("environment_probe_authorize")
    if authorize is not None:
        await authorize(plan)
    inspection = SourceInspection(sorted(plan.inputs), fetch, limits, budget)
    evidence = await prepare_mandatory_evidence(inspection, plan.inputs, phase="probe")
    budget.stage = "probe_execution"
    report = await execute_environment_probe(plan, state=state,
        source_files={item["path"]:item["content"] for item in evidence}, budget=budget)
    validate_probe_report(plan, report)
    sanitized = report.model_copy(update={"results":[item.model_copy(update={"output":redact_command_text(item.output)})
        for item in report.results]})
    sanitized = ProbeReport.model_validate(sanitized.model_dump())
    finish_probe(cache, sanitized)
    await budget.flush()
    return sanitized
