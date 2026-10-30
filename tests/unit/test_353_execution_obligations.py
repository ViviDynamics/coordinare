"""Actual execution freezes required command definitions across environment repairs."""

from __future__ import annotations

import copy
import json

import pytest
from coordinare_service_inference.environment.models import EnvironmentManifest

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_obligations import preserve_execution_obligations
from tests.unit.env_manifest_fixture import reviewed_metadata, verification_output


def contract():
    return EnvironmentManifest.model_validate(
        reviewed_metadata(
            checks=[
                {"id": "suite", "argv": ["quality-runner", "suite"]},
                {"id": "assets", "argv": ["artifact-check"]},
            ],
        )["environment_manifest"],
    )


def history(manifest, *, preflight=False, install_started=False):
    report = json.loads(verification_output(manifest.model_dump()))
    for result in report["results"]:
        result.update(
            status="fail" if result["id"] == "suite" else "not_run",
            exit_code=None if preflight else (1 if result["id"] == "suite" else None),
            duration_seconds=0 if preflight else (1 if result["id"] == "suite" else 0),
        )
    if install_started:
        report["results"].append(
            {
                "id": "install",
                "status": "pass",
                "required": True,
                "exit_code": 0,
                "duration_seconds": 1,
                "manifest_digest": manifest.digest(),
            },
        )
    return [
        {
            "manifest_digest": manifest.digest(),
            "manifest": manifest.model_dump(mode="json"),
            "report": report,
        },
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("argv", ["quality-runner", "suite", "--omit", "failed-case"]),
        ("argv", ["true"]),
        ("cwd", "{repo}/other"),
        ("env", {"QUALITY_MODE": "ignore"}),
        ("depends_on", ["assets"]),
        ("scope", ["qa"]),
        ("required", False),
        ("timeout_seconds", 1),
    ],
)
def test_executed_required_definition_cannot_be_weakened(field, value):
    previous = contract()
    data = previous.model_dump()
    data["checks"][0][field] = value
    # Exercise the frozen-command guard directly even when schema admission
    # would already reject requiredness/scope weakening.
    changed = previous.model_copy(deep=True)
    changed.checks[0] = changed.checks[0].model_copy(update={field: value})
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        preserve_execution_obligations(changed, history(previous))
    assert exc.value.code == "obligation_removed"


def test_unrun_required_check_is_pinned_once_another_action_ran():
    previous = contract()
    data = previous.model_dump()
    data["checks"][1]["argv"] = ["true"]
    with pytest.raises(EnvironmentDiscoveryError):
        preserve_execution_obligations(EnvironmentManifest.model_validate(data), history(previous))


def test_installer_execution_pins_unrun_required_verification():
    data = contract().model_dump()
    data["install"] = [{"id": "install", "argv": ["provision-tools"]}]
    previous = EnvironmentManifest.model_validate(data)
    data = previous.model_dump()
    data["checks"][0]["argv"] = ["true"]
    with pytest.raises(EnvironmentDiscoveryError):
        preserve_execution_obligations(
            EnvironmentManifest.model_validate(data),
            history(previous, preflight=True, install_started=True),
        )


def test_template_preflight_before_any_command_may_correct_definitions():
    previous = contract()
    data = previous.model_dump()
    data["checks"][0]["argv"] = ["corrected-check"]
    preserve_execution_obligations(
        EnvironmentManifest.model_validate(data), history(previous, preflight=True),
    )


def test_environment_repairs_and_longer_deadline_preserve_the_command():
    previous = contract()
    data = previous.model_dump()
    data["checks"][0]["timeout_seconds"] += 30
    data["activation"] = {"path": ["{cache}/tools"], "env": {"TOOL_HOME": "{cache}/tools"}}
    data["install"] = [{"id": "install-tools", "argv": ["provision-tools", "{cache}/tools"]}]
    preserve_execution_obligations(EnvironmentManifest.model_validate(data), history(previous))


@pytest.mark.parametrize("phase", ["init", "start", "health", "stop"])
def test_required_service_lifecycle_definitions_are_pinned(phase):
    data = contract().model_dump()
    data["services"] = [
        {
            "id": "backend",
            "required": True,
            **{
                p: {"id": "backend-" + p, "argv": ["backend-tool", p]}
                for p in ["init", "start", "health", "stop"]
            },
        },
    ]
    previous = EnvironmentManifest.model_validate(data)
    data = previous.model_dump()
    data["services"][0][phase]["argv"] = ["true"]
    with pytest.raises(EnvironmentDiscoveryError):
        preserve_execution_obligations(EnvironmentManifest.model_validate(data), history(previous))


def test_every_prior_execution_remains_binding():
    previous = contract()
    records = history(previous)
    later = previous.model_dump()
    later["checks"].append({"id": "new-obligation", "argv": ["new-check"]})
    later = EnvironmentManifest.model_validate(later)
    records += history(later)
    changed = later.model_dump()
    changed["checks"][0]["argv"] = ["true"]
    with pytest.raises(EnvironmentDiscoveryError):
        preserve_execution_obligations(EnvironmentManifest.model_validate(changed), records)


@pytest.mark.parametrize(
    "mutation",
    ["manifest_digest", "source", "report_digest", "result_digest", "duplicate", "missing"],
)
def test_unknown_or_unbound_history_holds(mutation):
    previous = contract()
    records = history(previous)
    if mutation == "manifest_digest":
        records[0]["manifest_digest"] = "f" * 64
    elif mutation == "source":
        records[0]["manifest"]["source_commit"] = "f" * 40
    elif mutation == "report_digest":
        records[0]["report"]["manifest_digest"] = "f" * 64
    elif mutation == "result_digest":
        records[0]["report"]["results"][0]["manifest_digest"] = "f" * 64
    elif mutation == "duplicate":
        records[0]["report"]["results"].append(copy.deepcopy(records[0]["report"]["results"][0]))
    else:
        del records[0]["report"]
    with pytest.raises(EnvironmentDiscoveryError):
        preserve_execution_obligations(previous, records)


def test_restarted_checkpoint_retains_executed_command_pins(tmp_path):
    from types import SimpleNamespace

    from coordinare.daemon import _persist_env_cache, _rehydrate_env_cache
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import (
        begin_attempt,
        prepare_repair,
        record_execution_failure,
        save_attempt,
    )
    from tests.unit.test_353_execution_repair import cache_fixture, failed_report, started

    cache = cache_fixture(tmp_path)
    started(cache)
    assert record_execution_failure(cache, failed_report(cache), cleanup_confirmed=True, now=101)
    previous, _ = prepare_repair(cache)
    restored = cache_fixture(tmp_path)
    _rehydrate_env_cache(
        {"env_cache": {"suite": restored}},
        SimpleNamespace(env_cache=_persist_env_cache({"suite": cache})),
    )
    budget = begin_attempt(restored, DiscoveryLimits(), outer_seconds=3600, now=102)
    data = previous.model_dump()
    data["checks"][0]["argv"] = ["replacement-pass"]
    budget.coverage_manifest = EnvironmentManifest.model_validate(data)
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        save_attempt(restored, budget)
    assert exc.value.code == "obligation_removed"
    assert restored.environment_attempt["execution_repairs"] == 1


def test_saved_acceptance_cannot_reuse_waived_execution(tmp_path):
    from agent.performer.tests.source_contract_fixture import current_review_fixture

    from coordinare.services.environment_acceptance import accepted_environment
    from coordinare.services.environment_repair import record_execution_failure
    from tests.unit.test_353_execution_repair import cache_fixture, failed_report, started

    cache = cache_fixture(tmp_path)
    started(cache)
    assert record_execution_failure(cache, failed_report(cache), cleanup_confirmed=True, now=101)
    data = copy.deepcopy(cache.environment_manifest)
    data["checks"][0]["argv"] = ["replacement-pass"]
    changed = EnvironmentManifest.model_validate(data)
    cache.environment_manifest = changed.model_dump()
    review = {**cache.environment_assessment, "manifest_digest": changed.digest()}
    cache.environment_assessment = current_review_fixture(changed, review)
    assert accepted_environment(cache) is None


@pytest.mark.asyncio
async def test_discovery_rejects_waiver_before_fresh_review(tmp_path):
    import hashlib

    from agent.performer.tests.source_contract_fixture import current_review_fixture
    from coordinare_service_inference.environment.models import ExecutionCapabilities

    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import (
        begin_attempt,
        prepare_repair,
        record_execution_failure,
    )
    from tests.unit.env_manifest_fixture import manifest_proposal
    from tests.unit.scope_chat_fixture import discover_with_fixture_scope as discover_and_review
    from tests.unit.test_353_discovery_repair import Replies
    from tests.unit.test_353_execution_repair import cache_fixture, failed_report, started

    cache = cache_fixture(tmp_path)
    from agent.performer.tests.source_contract_fixture import current_manifest_fixture

    original = current_manifest_fixture(
        {
            **cache.environment_manifest,
            "inventory_digest": hashlib.sha256(b'["tools.plan"]').hexdigest(),
        },
    )
    cache.environment_manifest = original.model_dump()
    cache.environment_assessment = current_review_fixture(
        original,
        {
            **cache.environment_assessment,
            "manifest_digest": original.digest(),
            "inventory_digest": original.inventory_digest,
        },
    )
    started(cache)
    assert record_execution_failure(cache, failed_report(cache), cleanup_confirmed=True, now=101)
    previous, _ = prepare_repair(cache)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=102)
    changed = previous.model_dump()
    changed["checks"][0]["argv"] = ["replacement-pass"]
    replies = Replies([{"manifest": manifest_proposal(changed)}])

    async def fetch(path):
        return "baseline"

    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await discover_and_review(
            symphony_name=cache.symphony_name,
            commit=previous.source_commit,
            paths=["tools.plan"],
            fetch=fetch,
            chat=replies,
            budget=budget,
            previous_manifest=previous,
            capabilities=ExecutionCapabilities.model_validate(cache.environment_capabilities),
        )
    assert exc.value.code == "obligation_removed"
    assert len(replies.messages) == 1


def test_every_changed_fixed_command_is_named_in_one_correction():
    """Dettmore e2e-18: told of one changed command at a time, the author restored it and changed the next until the
    schema corrections ran out."""
    previous = contract()
    changed = previous.model_copy(deep=True)
    changed.checks[0] = changed.checks[0].model_copy(update={"argv": ["true"]})
    changed.checks[1] = changed.checks[1].model_copy(update={"argv": ["true"]})
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        preserve_execution_obligations(changed, history(previous))
    assert exc.value.code == "obligation_removed"
    assert str(exc.value).endswith(": suite, assets")
    one = previous.model_copy(deep=True)
    one.checks[0] = one.checks[0].model_copy(update={"argv": ["true"]})
    with pytest.raises(EnvironmentDiscoveryError) as single:
        preserve_execution_obligations(one, history(previous))
    assert str(single.value) == "required executable command suite changed after project execution"
