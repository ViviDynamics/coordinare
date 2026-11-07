"""Shared durable ownership and independent evidence for daemon/harness probes."""

from __future__ import annotations

import asyncio
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coordinare.lib.redaction import redact_command_text
from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_inspection import SourceInspection
from coordinare.services.environment_probe_executor import execute_environment_probe
from coordinare.services.environment_probes import (
    ProbePlan,
    ProbeReport,
    probe_batch_timeout,
    validate_probe_report,
)
from coordinare.services.environment_repair import abandon_probe, finish_probe, reserve_probe
from coordinare.services.environment_source_provider import (
    RepositorySourceRequest,
    prepare_repository_source,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from coordinare_service_inference.environment.execution_source import ExecutionSourceBundle

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
    source_bundle: ExecutionSourceBundle | None = None


async def _run_batch(plan: ProbePlan, context: ProbeServiceContext) -> ProbeReport:
    """Prepare the probe's source and run it within the batch allowance."""
    cache, state, budget = context.cache, context.state, context.budget
    deadline = min(budget.deadline, time.monotonic() + probe_batch_timeout(plan))
    try:
        async with asyncio.timeout_at(deadline):
            budget.stage = "probe_execution"
            with tempfile.TemporaryDirectory(prefix="coordinare-probe-source-") as temporary:
                bundle = context.source_bundle
                if bundle is None:
                    bundle = await prepare_repository_source(state, RepositorySourceRequest(
                        cache.symphony_name, plan.source_commit, plan.inputs), destination=Path(temporary)/'source',
                        deadline=deadline)
                return await execute_environment_probe(
                    plan, state=state, source_bundle=bundle, budget=budget, deadline=deadline,
                )
    except TimeoutError:
        # Its own code, so a hold names the probe batch rather than the phase it ran in (e2e-65 read as phase_deadline).
        budget.check()
        message = "probe batch deadline exceeded"
        raise EnvironmentDiscoveryError(message, code="probe_batch_deadline") from None


async def run_owned_probe(plan: ProbePlan, *, context: ProbeServiceContext) -> ProbeReport:
    cache, state, fetch, budget, limits = (
        context.cache,
        context.state,
        context.fetch,
        context.budget,
        context.limits,
    )
    reserve_probe(cache, plan)
    authorize = state.get("environment_probe_authorize")
    if authorize is not None:
        budget.stage = "probe_authorization"
    await budget.flush()
    if authorize is not None:
        try:
            await authorize(plan)
        except BaseException:
            # No source preparation or executor call has occurred at this boundary.
            abandon_probe(cache, budget, plan, reason="authorization_interrupted", cleanup_confirmed=True)
            await budget.flush()
            raise
    budget.stage = "probe_preparation"
    inspection = SourceInspection(sorted(plan.inputs), fetch, limits, budget)
    # Probe verification has no model conversation. Bind the complete input set under the existing verifier bounds and
    # charge every read to the attempt, before the batch allowance starts: Dettmore e2e-65 re-verified 595 inputs inside
    # the 120 s batch on a loaded host and held with time left. The batch allowance covers preparing and running the probe.
    async with asyncio.timeout_at(budget.deadline):
        await inspection.bind_verified(plan.inputs, phase="probe")
    report = await _run_batch(plan, context)
    validate_probe_report(plan, report)
    sanitized = report.model_copy(
        update={
            "results": [
                item.model_copy(update={"output": redact_command_text(item.output)})
                for item in report.results
            ],
        },
    )
    sanitized = ProbeReport.model_validate(sanitized.model_dump())
    finish_probe(cache, sanitized)
    await budget.flush()
    return sanitized
