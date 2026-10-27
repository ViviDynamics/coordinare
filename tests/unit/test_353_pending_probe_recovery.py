"""Interrupted synthetic probes keep their spend without becoming observations."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from coordinare.models.env_cache import EnvCacheState
from coordinare.services import environment_probe_service as service
from coordinare.services import environment_repair as repair
from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_probes import ProbePlan, ProbeRequest
from coordinare.services.environment_repair import begin_attempt, save_attempt
from tests.unit.test_353_discovery_repair import CAPABILITIES
from tests.unit.test_353_scope_contract import (
    COMMIT,
    SOURCES,
    Replies,
    criterion,
    proposal,
    review,
    run,
)


async def pending_probe(tmp_path):
    cache = EnvCacheState(
        symphony_name="sample",
        sanitised_name="sample",
        cache_dir=tmp_path / "sample",
        bootstrap_commit_sha=COMMIT,
    )
    limits = DiscoveryLimits()
    budget = begin_attempt(cache, limits, outer_seconds=600)

    async def checkpoint():
        save_attempt(cache, budget)

    budget.checkpoint = checkpoint
    scope = await run(
        Replies([{"tool": "read_file", "path": "guide"}, proposal(criterion()), review()]),
        budget=budget,
    )
    selected = ProbePlan(
        execution_source_digest="1" * 64,
        execution_id="1" * 32,
        request=ProbeRequest.model_validate(
            {
                "commands": [{"id": "inspect", "argv": ["synthetic-tool", "--version"]}],
                "citations": {"inspect": ["guide"]},
                "questions": {"inspect": "Is the synthetic tool present?"},
            },
        ),
        source_commit=COMMIT,
        inventory_digest=scope.definition.inventory_digest,
        inputs=dict(scope.all_inputs),
        capabilities=CAPABILITIES,
    )
    budget.reserve_probe_batch()
    await budget.flush()
    return cache, budget, limits, selected


@pytest.mark.asyncio
async def test_authorization_timeout_releases_owner_without_refunding_batch(tmp_path, monkeypatch):
    cache, budget, limits, selected = await pending_probe(tmp_path)
    original = copy.deepcopy(cache.environment_attempt)

    async def authorize(_plan):
        assert cache.environment_attempt["diagnostics"]["stage"] == "probe_authorization"
        raise TimeoutError

    async def forbidden(*_args, **_kwargs):
        pytest.fail("Authorization failure must precede source preparation and execution")

    monkeypatch.setattr(service, "execute_environment_probe", forbidden)
    monkeypatch.setattr(service, "prepare_repository_source", forbidden)
    context = service.ProbeServiceContext(
        cache,
        {"environment_probe_authorize": authorize},
        forbidden,
        budget,
        limits,
    )
    with pytest.raises(TimeoutError):
        await service.run_owned_probe(selected, context=context)

    saved = cache.environment_attempt
    assert saved["probe_owner"] is None
    assert saved["probe_plan"] is None
    assert saved["probe_reports"] == []
    assert saved["probe_batches"] == budget.probe_batches == 1
    assert saved["expires_at"] == original["expires_at"]
    assert saved["calls"] == original["calls"]
    assert saved["reads"] == original["reads"]
    assert len(saved["probe_abandonments"]) == 1
    assert saved["probe_abandonments"][0]["plan"] == selected.model_dump(mode="json")
    assert json.loads(json.dumps(saved))["probe_batches"] == 1


async def held_legacy_probe(tmp_path):
    cache, budget, limits, selected = await pending_probe(tmp_path)
    repair.reserve_probe(cache, selected)
    budget.stage = "probe_execution"
    budget.error_code = "request_failed"
    save_attempt(cache, budget)
    cache.environment_attempt.pop("probe_abandonments", None)
    return cache, budget, limits, selected


@pytest.mark.asyncio
async def test_legacy_recovery_preserves_spend_and_allows_fresh_image_probe(tmp_path, monkeypatch):
    from coordinare.services import environment_discovery as discovery
    from coordinare.services.environment_probes import ProbeReport
    from tests.unit.test_353_execution_source_pipeline import bundle_fixture

    cache, _budget, limits, old = await held_legacy_probe(tmp_path)
    original = copy.deepcopy(cache.environment_attempt)
    repair.recover_pending_probe(
        cache,
        expected_plan_digest=old.digest(),
        cleanup_confirmed=True,
        now=original["started_at"] + 2,
    )
    recovered = cache.environment_attempt
    assert recovered["status"] == "discovering" and recovered["last_error"] is None
    assert recovered["probe_owner"] is None and recovered["probe_plan"] is None
    for key in (
        "expires_at",
        "started_at",
        "calls",
        "reads",
        "scope_revisions",
        "coverage_revisions",
        "execution_repairs",
        "scope_contract",
        "scope_findings",
        "finding_ledger",
    ):
        assert recovered[key] == original[key]
    assert recovered["probe_batches"] == 1 and recovered["probe_reports"] == []
    assert recovered["probe_abandonments"][0]["plan"] == old.model_dump(mode="json")
    assert recovered["probe_abandonments"][0]["error_code"] == "request_failed"

    cache.environment_attempt = json.loads(json.dumps(recovered))
    resumed = begin_attempt(cache, limits, outer_seconds=3600, now=original["started_at"] + 3)
    assert resumed.probe_batches == 1 and len(resumed.probe_abandonments) == 1

    async def checkpoint():
        save_attempt(cache, resumed)

    resumed.checkpoint = checkpoint
    fresh_capabilities = CAPABILITIES.model_copy(
        update={"image_ref": "registry.example/fresh@sha256:" + "f" * 64},
    )
    authorized = []

    async def authorize(plan):
        authorized.append(plan.digest())
        assert plan.execution_id != old.execution_id
        assert plan.capabilities.image_ref == fresh_capabilities.image_ref
        assert cache.environment_attempt["probe_batches"] == 2

    async def fetch(path):
        return SOURCES[path]

    async def execute(plan, **_kwargs):
        assert authorized == [plan.digest()]
        return ProbeReport.model_validate(
            {
                "schema_version": 1,
                "execution_source_digest": plan.execution_source_digest,
                "plan_digest": plan.digest(),
                "source_commit": COMMIT,
                "image_ref": plan.capabilities.image_ref,
                "platform": plan.capabilities.platform,
                "results": [
                    {
                        "id": "inspect",
                        "status": "completed",
                        "exit_code": 127,
                        "output": "Synthetic tool absent",
                        "truncated": False,
                    },
                ],
                "cleanup_confirmed": True,
            },
        )

    monkeypatch.setattr(service, "execute_environment_probe", execute)
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    bundle = bundle_fixture(
        bundle_root, {key: (value.encode(), "100644") for key, value in SOURCES.items()},
    )

    async def owned(plan):
        return await service.run_owned_probe(
            plan,
            context=service.ProbeServiceContext(
                cache,
                {"environment_probe_authorize": authorize},
                fetch,
                resumed,
                limits,
                bundle,
            ),
        )

    inspection = discovery._Inspection("discovery", list(SOURCES), fetch, limits, resumed)
    inspection.inputs.update(old.inputs)
    options = discovery._DiscoveryOptions(
        budget=resumed,
        capabilities=fresh_capabilities,
        probe_executor=owned,
        execution_source_digest=bundle.identity.digest(),
    )
    observed = await discovery._probe_handler(inspection, options, COMMIT)(
        old.request.model_dump(mode="json"),
    )
    await resumed.flush()
    assert observed.plan_digest == authorized[0] != old.digest()
    assert cache.environment_attempt["probe_batches"] == resumed.probe_batches == 2
    assert len(cache.environment_attempt["probe_reports"]) == 1
    assert len(cache.environment_attempt["probe_abandonments"]) == 1
    assert cache.environment_attempt["probe_owner"] is None
    assert cache.environment_attempt["expires_at"] == original["expires_at"]
    assert cache.environment_attempt["calls"] == original["calls"]
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        resumed.reserve_probe_batch()
    assert exc.value.code == "attempt_probe_limit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "cleanup",
        "expired",
        "backwards",
        "source",
        "cache",
        "active",
        "source_owner",
        "target",
        "manifest",
        "reports",
        "generation",
        "calls",
        "reads",
        "stage",
    ],
)
async def test_recovery_refusal_does_not_mutate_the_checkpoint(tmp_path, change):
    cache, _budget, _limits, selected = await held_legacy_probe(tmp_path)
    now = cache.environment_attempt["started_at"] + 2
    expected = selected.digest()
    if change == "digest":
        expected = "f" * 64
    elif change == "expired":
        now = cache.environment_attempt["expires_at"]
    elif change == "backwards":
        now = cache.environment_attempt["started_at"] - 1
    elif change == "source":
        cache.bootstrap_commit_sha = "b" * 40
    elif change == "cache":
        cache.cache_dir = tmp_path / "different"
    elif change == "active":
        cache.bootstrap_in_flight = True
    elif change == "source_owner":
        cache.environment_source_owner = {"opaque": "active"}
    elif change == "target":
        cache.environment_attempt["target"] = CAPABILITIES.model_dump(mode="json")
    elif change == "manifest":
        cache.environment_manifest = {"present": True}
    elif change == "reports":
        cache.environment_attempt["probe_reports"] = [{"plan_digest": "d" * 64}]
    elif change == "generation":
        cache.environment_attempt["generation"] = 1
    elif change == "calls":
        cache.environment_attempt["calls"] = cache.environment_attempt["max_calls"]
    elif change == "reads":
        cache.environment_attempt["reads"] = cache.environment_attempt["max_reads"]
    elif change == "stage":
        cache.environment_attempt["diagnostics"]["stage"] = "scope_review_preparation"
    before = copy.deepcopy(cache.environment_attempt)
    with pytest.raises(EnvironmentDiscoveryError):
        repair.recover_pending_probe(
            cache, expected_plan_digest=expected, cleanup_confirmed=change != "cleanup", now=now,
        )
    assert cache.environment_attempt == before


@pytest.mark.asyncio
async def test_stale_budget_cannot_remove_abandonment_history(tmp_path):
    cache, budget, limits, selected = await pending_probe(tmp_path)

    async def denied(_plan):
        raise TimeoutError

    async def forbidden(_path):
        pytest.fail("No execution after failed authorization")

    with pytest.raises(TimeoutError):
        await service.run_owned_probe(
            selected,
            context=service.ProbeServiceContext(
                cache,
                {"environment_probe_authorize": denied},
                forbidden,
                budget,
                limits,
            ),
        )
    before = copy.deepcopy(cache.environment_attempt)
    budget.probe_abandonments = []
    with pytest.raises(EnvironmentDiscoveryError):
        save_attempt(cache, budget)
    assert cache.environment_attempt == before


@pytest.mark.asyncio
async def test_duplicate_abandonment_is_not_a_second_consumed_slot(tmp_path):
    cache, budget, limits, selected = await pending_probe(tmp_path)

    async def denied(_plan):
        raise TimeoutError

    async def forbidden(_path):
        pytest.fail("No execution after failed authorization")

    with pytest.raises(TimeoutError):
        await service.run_owned_probe(
            selected,
            context=service.ProbeServiceContext(
                cache,
                {"environment_probe_authorize": denied},
                forbidden,
                budget,
                limits,
            ),
        )
    cache.environment_attempt["probe_abandonments"] *= 2
    with pytest.raises(EnvironmentDiscoveryError):
        repair.journal_for(cache)


@pytest.mark.asyncio
async def test_cancelled_authorization_never_starts_executor(tmp_path, monkeypatch):
    cache, budget, limits, selected = await pending_probe(tmp_path)
    entered = asyncio.Event()

    async def authorize(_plan):
        entered.set()
        await asyncio.Event().wait()

    async def forbidden(*_args, **_kwargs):
        pytest.fail("Cancelled authorization must not reach the executor")

    monkeypatch.setattr(service, "execute_environment_probe", forbidden)
    task = asyncio.create_task(
        service.run_owned_probe(
            selected,
            context=service.ProbeServiceContext(
                cache,
                {"environment_probe_authorize": authorize},
                forbidden,
                budget,
                limits,
            ),
        ),
    )
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cache.environment_attempt["probe_owner"] is None
    assert len(cache.environment_attempt["probe_abandonments"]) == 1
    assert cache.environment_attempt["probe_batches"] == 1
    assert cache.environment_attempt["probe_reports"] == []


@pytest.mark.asyncio
async def test_executor_failure_keeps_ambiguous_ownership(tmp_path, monkeypatch):
    from tests.unit.test_353_execution_source_pipeline import bundle_fixture

    cache, budget, limits, selected = await pending_probe(tmp_path)
    root = tmp_path / "bundle"
    root.mkdir()
    bundle = bundle_fixture(
        root, {key: (value.encode(), "100644") for key, value in SOURCES.items()},
    )

    async def fetch(path):
        return SOURCES[path]

    async def execute(*_args, **_kwargs):
        raise RuntimeError("Synthetic ambiguous executor failure")

    monkeypatch.setattr(service, "execute_environment_probe", execute)
    with pytest.raises(RuntimeError):
        await service.run_owned_probe(
            selected,
            context=service.ProbeServiceContext(
                cache,
                {},
                fetch,
                budget,
                limits,
                bundle,
            ),
        )
    assert cache.environment_attempt["probe_owner"] == selected.digest()
    assert cache.environment_attempt["probe_abandonments"] == []
    assert cache.environment_attempt["probe_reports"] == []


@pytest.mark.asyncio
async def test_late_report_cannot_complete_an_abandoned_reservation(tmp_path):
    from coordinare.services.environment_probes import ProbeReport

    cache, budget, limits, selected = await pending_probe(tmp_path)

    async def denied(_plan):
        raise TimeoutError

    async def forbidden(_path):
        pytest.fail("No executor after authorization failure")

    with pytest.raises(TimeoutError):
        await service.run_owned_probe(
            selected,
            context=service.ProbeServiceContext(
                cache,
                {"environment_probe_authorize": denied},
                forbidden,
                budget,
                limits,
            ),
        )
    report = ProbeReport.model_validate(
        {
            "schema_version": 1,
            "execution_source_digest": selected.execution_source_digest,
            "plan_digest": selected.digest(),
            "source_commit": COMMIT,
            "image_ref": selected.capabilities.image_ref,
            "platform": selected.capabilities.platform,
            "results": [
                {
                    "id": "inspect",
                    "status": "completed",
                    "exit_code": 0,
                    "output": "Late synthetic result",
                    "truncated": False,
                },
            ],
            "cleanup_confirmed": True,
        },
    )
    before = copy.deepcopy(cache.environment_attempt)
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        repair.finish_probe(cache, report)
    assert exc.value.code == "probe_report_invalid"
    assert cache.environment_attempt == before


@pytest.mark.asyncio
async def test_second_abandonment_exhausts_the_existing_probe_allowance(tmp_path):
    cache, budget, limits, selected = await pending_probe(tmp_path)

    async def denied(_plan):
        raise TimeoutError

    async def forbidden(_path):
        pytest.fail("No executor after authorization failure")

    context = service.ProbeServiceContext(
        cache, {"environment_probe_authorize": denied}, forbidden, budget, limits,
    )
    with pytest.raises(TimeoutError):
        await service.run_owned_probe(selected, context=context)
    budget.reserve_probe_batch()
    await budget.flush()
    second = selected.model_copy(update={"execution_id": "2" * 32})
    with pytest.raises(TimeoutError):
        await service.run_owned_probe(second, context=context)
    budget.error_code = "request_failed"
    save_attempt(cache, budget)
    before = copy.deepcopy(cache.environment_attempt)
    with pytest.raises(EnvironmentDiscoveryError):
        repair.recover_pending_probe(
            cache, expected_plan_digest=second.digest(), cleanup_confirmed=True,
        )
    assert cache.environment_attempt == before
    assert cache.environment_attempt["probe_batches"] == 2
    assert len(cache.environment_attempt["probe_abandonments"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["cleanup", "source", "timestamp", "overlap", "counter", "owner"],
)
async def test_invalid_abandonment_history_fails_closed(tmp_path, change):
    cache, budget, limits, selected = await pending_probe(tmp_path)

    async def denied(_plan):
        raise TimeoutError

    async def forbidden(_path):
        pytest.fail("No execution after failed authorization")

    with pytest.raises(TimeoutError):
        await service.run_owned_probe(
            selected,
            context=service.ProbeServiceContext(
                cache,
                {"environment_probe_authorize": denied},
                forbidden,
                budget,
                limits,
            ),
        )
    saved = cache.environment_attempt
    record = saved["probe_abandonments"][0]
    if change == "cleanup":
        record["cleanup_confirmed"] = 1
    elif change == "source":
        record["plan"]["source_commit"] = "b" * 40
        record["plan_digest"] = ProbePlan.model_validate(record["plan"]).digest()
    elif change == "timestamp":
        record["recorded_at"] = 0.0
    elif change == "overlap":
        saved["probe_reports"] = [{"plan_digest": selected.digest()}]
    elif change == "counter":
        saved["probe_batches"] = 0
    elif change == "owner":
        saved["probe_owner"] = selected.digest()
        saved["probe_plan"] = selected.model_dump(mode="json")
    with pytest.raises(EnvironmentDiscoveryError):
        repair.journal_for(cache)
