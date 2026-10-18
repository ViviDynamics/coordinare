"""Corrections preserve untouched proposal records and all existing admission gates."""

from __future__ import annotations

import copy
import json

import pytest

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from tests.unit.test_353_discovery_repair import Replies, assessment, candidate, run


@pytest.mark.asyncio
async def test_invalid_reference_can_be_corrected_without_reserializing_other_commands():
    from tests.unit.test_351_environment_discovery import PATH

    invalid = candidate()
    invalid["requirements"][0]["checks"] = ["missing"]
    untouched = copy.deepcopy(invalid["checks"])
    replies = Replies(
        [
            {"tool": "read_file", "path": PATH},
            {"manifest": invalid},
            {
                "tool": "repair_proposal",
                "changes": [
                    {
                        "path": ["manifest", "requirements", 0, "checks"],
                        "value_json": '["version"]',
                    },
                ],
            },
            {"tool": "read_file", "path": PATH},
            assessment(candidate()),
        ],
    )
    budget = AttemptBudget()
    manifest, review = await run(replies, budget=budget)
    assert manifest.checks[0].model_dump()["argv"] == untouched[0]["argv"]
    assert manifest.requirements[0].checks == ["version"]
    assert review.manifest_digest == manifest.digest()
    assert budget.schema_repairs == 1


@pytest.mark.asyncio
async def test_patch_cannot_remove_required_obligations():
    from tests.unit.test_351_environment_discovery import PATH

    invalid = candidate()
    invalid["requirements"][0]["checks"] = ["missing"]
    replies = Replies(
        [
            {"tool": "read_file", "path": PATH},
            {"manifest": invalid},
            {
                "tool": "repair_proposal",
                "changes": [{"path": ["manifest", "checks"], "value_json": "[]"}],
            },
        ],
    )
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run(replies)
    assert caught.value.code == "obligation_removed"


@pytest.mark.asyncio
async def test_invented_finding_id_feedback_has_actual_ledger_ids():
    from tests.unit.test_351_environment_discovery import PATH

    fake = {"finding_id": "F0001", "sources": [PATH], "affected_ids": [], "rationale": "invented"}
    replies = Replies(
        [
            {"tool": "read_file", "path": PATH},
            {"manifest": candidate(), "finding_responses": [fake]},
            {
                "tool": "repair_proposal",
                "changes": [{"path": ["finding_responses"], "value_json": "[]"}],
            },
            {"tool": "read_file", "path": PATH},
            assessment(candidate()),
        ],
    )
    await run(replies)
    feedback = json.loads(replies.messages[2][-1]["content"])
    error = feedback["validation_errors"][0]
    assert error["location"] == ["finding_responses"]
    assert error["allowed_ids"] == []
    assert error["required_ids"] == []


def test_patch_is_atomic_and_bounded():
    from coordinare.services.environment_proposal_patch import apply_patch

    base = {"manifest": candidate()}
    original = copy.deepcopy(base)
    patch = {
        "tool": "repair_proposal",
        "changes": [
            {"path": ["manifest", "checks", 0, "argv"], "value_json": '["false"]'},
            {"path": ["manifest", "checks", 999, "argv"], "value_json": '["true"]'},
        ],
    }
    with pytest.raises(EnvironmentDiscoveryError):
        apply_patch(base, patch, max_bytes=262144)
    assert base == original


@pytest.mark.parametrize(
    "path",
    [
        [],
        ["source_commit"],
        ["manifest", "checks", True],
        ["manifest", "checks", -1],
        ["manifest", "missing", "argv"],
    ],
)
def test_patch_cannot_escape_existing_proposal_tree(path):
    from coordinare.services.environment_proposal_patch import apply_patch

    with pytest.raises(EnvironmentDiscoveryError):
        apply_patch(
            {"manifest": candidate()},
            {"tool": "repair_proposal", "changes": [{"path": path, "value_json": "[]"}]},
            max_bytes=262144,
        )


@pytest.mark.parametrize("encoded", ['{"x":1,"x":2}', "NaN", "[Infinity]"])
def test_duplicate_or_nonfinite_replacement_json_is_rejected(encoded):
    from coordinare.services.environment_proposal_patch import apply_patch

    with pytest.raises(EnvironmentDiscoveryError):
        apply_patch(
            {"manifest": candidate()},
            {
                "tool": "repair_proposal",
                "changes": [{"path": ["manifest", "activation"], "value_json": encoded}],
            },
            max_bytes=262144,
        )


def test_patch_cannot_bypass_output_bound():
    from coordinare.services.environment_proposal_patch import apply_patch

    with pytest.raises(EnvironmentDiscoveryError):
        apply_patch(
            {"manifest": candidate()},
            {
                "tool": "repair_proposal",
                "changes": [{"path": ["manifest", "empty_reason"], "value_json": '"oversized"'}],
            },
            max_bytes=1,
        )


@pytest.mark.asyncio
async def test_missing_review_field_can_be_patched_with_same_budget_and_admission():
    from tests.unit.test_351_environment_discovery import PATH

    valid = assessment(candidate())
    invalid = copy.deepcopy(valid)
    invalid["assessment"].pop("capability_unavailable")
    replies = Replies(
        [
            {"tool": "read_file", "path": PATH},
            {"manifest": candidate()},
            {"tool": "read_file", "path": PATH},
            invalid,
            {
                "tool": "repair_proposal",
                "changes": [
                    {"path": ["assessment", "capability_unavailable"], "value_json": "false"},
                ],
            },
        ],
    )
    budget = AttemptBudget()
    manifest, review = await run(replies, budget=budget)
    assert review.manifest_digest == manifest.digest()
    assert budget.schema_repairs == 1


@pytest.mark.asyncio
async def test_patch_without_unambiguous_proposal_cannot_establish_a_candidate():
    from tests.unit.test_351_environment_discovery import PATH

    replies = Replies(
        [
            {"tool": "read_file", "path": PATH},
            {
                "tool": "repair_proposal",
                "changes": [{"path": ["manifest"], "value_json": json.dumps(candidate())}],
            },
        ],
    )
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run(replies, budget=AttemptBudget(max_schema_repairs=0))
    assert caught.value.code == "attempt_schema_limit"


def test_correction_schema_is_available_only_when_a_base_exists():
    import jsonschema

    from coordinare.services.environment_response_schema import response_schema
    from coordinare.services.environment_structured_wire import decode_wire, wire_schema

    request = {
        "tool": "repair_proposal",
        "changes": [{"path": ["manifest", "checks", 0, "depends_on"], "value_json": "[]"}],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(request, response_schema("manifest"))
    schema = response_schema("manifest", allow_patch=True)
    jsonschema.validate(request, schema)
    wire = {"response": request}
    jsonschema.validate(wire, wire_schema(schema))
    assert json.loads(decode_wire(json.dumps(wire), schema)) == request


@pytest.mark.asyncio
async def test_invalid_corrections_consume_original_shared_schema_allowance():
    from tests.unit.test_351_environment_discovery import PATH

    invalid = candidate()
    invalid["requirements"][0]["checks"] = ["missing"]
    bad = {
        "tool": "repair_proposal",
        "changes": [{"path": ["manifest", "checks", 999, "argv"], "value_json": "[]"}],
    }
    replies = Replies([{"tool": "read_file", "path": PATH}, {"manifest": invalid}, bad, bad])
    budget = AttemptBudget()
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run(replies, budget=budget)
    assert caught.value.code == "attempt_schema_limit"
    assert budget.schema_repairs == 2
    assert len(replies.messages) == 4
