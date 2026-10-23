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


@pytest.mark.asyncio
@pytest.mark.parametrize("complete_declaration", [False, True])
async def test_overfull_field_patch_reports_limits_and_can_replace_common_array(complete_declaration):
    from tests.unit.test_351_environment_discovery import PATH

    valid = candidate()
    valid["install"] = [{"id": "install-dev", "argv": ["true"]}]
    valid["checks"] = [
        {"id": f"version-{index}", "argv": ["zog", "--version"], "required": True}
        for index in range(22)
    ]
    valid["requirements"][0]["checks"] = [check["id"] for check in valid["checks"]]
    invalid = copy.deepcopy(valid)
    for check in invalid["checks"]:
        check["depends_on"] = ["install-dev"]
    overfull = {"tool": "repair_proposal", "changes": [
        {"path": ["manifest", "checks", index, "depends_on"], "value_json": "[]"}
        for index in range(22)
    ]}
    correction = {"manifest": valid} if complete_declaration else {
        "tool": "repair_proposal", "changes": [
            {"path": ["manifest", "checks"], "value_json": json.dumps(valid["checks"])},
        ],
    }
    replies = Replies([
        {"tool": "read_file", "path": PATH}, {"manifest": invalid}, overfull,
        correction,
        {"tool": "read_file", "path": PATH}, assessment(valid),
    ])
    budget = AttemptBudget()
    manifest, review = await run(replies, budget=budget)
    error = json.loads(replies.messages[3][-1]["content"])["validation_errors"][0]
    assert error["code"] == "invalid_proposal_patch"
    assert error["location"] == ["changes"]
    assert error["max_changes"] == 16
    assert error["max_path_depth"] == 8
    assert budget.schema_repairs == 2
    assert len(manifest.checks) == 22
    assert [check.id for check in manifest.checks] == valid["requirements"][0]["checks"]
    assert all(check.argv == ["zog", "--version"] and not check.depends_on and check.required
               for check in manifest.checks)
    assert review.manifest_digest == manifest.digest()


@pytest.mark.parametrize("phase", ["manifest", "assessment"])
def test_repair_schemas_offer_complete_declarations_to_both_structured_transports(phase):
    import jsonschema

    from coordinare.services.environment_discovery_prompts import (
        _ASSESSMENT_ENVELOPE,
        _MANIFEST_ENVELOPE,
    )
    from coordinare.services.environment_function_wire import decode_function, functions
    from coordinare.services.environment_response_schema import response_schema
    from coordinare.services.environment_structured_wire import decode_wire, wire_schema

    logical = (_MANIFEST_ENVELOPE.model_validate({"manifest": candidate(), "finding_responses": []})
               if phase == "manifest" else _ASSESSMENT_ENVELOPE.model_validate(assessment(candidate()))).model_dump(mode="json")
    schema = response_schema(phase, allow_patch=True)
    jsonschema.validate(logical, schema)
    declarations = {item["function"]["name"]: item["function"] for item in functions(schema)}
    name = f"submit_environment_{phase}"
    assert {"repair_proposal", "read_files", name} <= declarations.keys()
    arguments = ({**logical["manifest"], "finding_responses": logical["finding_responses"]}
                 if phase == "manifest" else logical["assessment"])
    jsonschema.validate(arguments, declarations[name]["parameters"])
    message = {"content": None, "tool_calls": [{"type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}]}
    assert json.loads(decode_function(json.dumps(message), schema)) == logical

    encoded = copy.deepcopy(logical)
    if phase == "manifest":
        encoded["manifest"]["activation"]["env"] = []
        encoded["manifest"]["criterion_checks"] = []
        for command in encoded["manifest"]["checks"]:
            command["env"] = []
    wire = {"response": encoded}
    jsonschema.validate(wire, wire_schema(schema))
    assert json.loads(decode_wire(json.dumps(wire), schema)) == logical
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**logical, "tool": "repair_proposal", "changes": []}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"tool": "repair_proposal", "changes": [
            {"path": [phase], "value_json": "{}"} for _ in range(17)
        ]}, schema)


@pytest.mark.asyncio
async def test_repair_feedback_path_can_be_applied_directly_to_complete_declaration():
    from tests.unit.test_351_environment_discovery import PATH

    invalid = candidate()
    invalid['checks'][0]['env'] = {'DATABASE_URL': 'postgresql://dev:fixture-placeholder@localhost/dev'}

    class FeedbackRepair(Replies):
        async def __call__(self, messages):
            if len(self.messages) == 2:
                feedback = json.loads(messages[-1]['content'])
                error = feedback['validation_errors'][0]
                path = error.get('correction_path', error['location'])
                assert path == ['manifest', 'checks', 0, 'env']
                self.messages.append(copy.deepcopy(messages))
                return json.dumps({'tool': 'repair_proposal', 'changes': [
                    {'path': path, 'value_json': '{}'}]})
            return await super().__call__(messages)

    replies = FeedbackRepair([{'tool': 'read_file', 'path': PATH}, {'manifest': invalid},
        {'tool': 'read_file', 'path': PATH}, assessment(candidate())])
    budget = AttemptBudget()
    manifest, accepted = await run(replies, budget=budget)
    assert manifest.checks[0].env == {}
    assert accepted.manifest_digest == manifest.digest() and budget.schema_repairs == 1


@pytest.mark.asyncio
async def test_a_plain_value_record_corrects_a_field_without_encoding_it_again():
    """v26: a shell command inside value_json is quoted three levels deep; the author never closed one."""
    from tests.unit.test_351_environment_discovery import PATH

    invalid = candidate()
    invalid["requirements"][0]["checks"] = ["missing"]
    replies = Replies([{"tool": "read_file", "path": PATH}, {"manifest": invalid},
                       {"tool": "repair_proposal", "changes": [{"path": ["manifest", "requirements", 0, "checks"],
                                                                 "value": ["version"]}]},
                       {"tool": "read_file", "path": PATH}, assessment(candidate())])
    manifest, _review = await run(replies, budget=AttemptBudget())
    assert manifest.requirements[0].checks == ["version"]


def test_a_record_with_both_value_forms_is_rejected():
    from coordinare.services.environment_proposal_patch import apply_patch

    request = {"tool": "repair_proposal", "changes": [{"path": ["manifest", "x"], "value": 1, "value_json": "1"}]}
    with pytest.raises(EnvironmentDiscoveryError):
        apply_patch({"manifest": {"x": 0}}, request, max_bytes=1024)


def test_only_a_prompt_described_schema_offers_plain_values():
    from coordinare.services.environment_proposal_patch import PATCH_SCHEMA, text_schema
    from coordinare.services.environment_response_schema import response_schema

    def records(schema):
        [patch] = [tool for tool in schema["anyOf"] if tool.get("properties", {}).get("tool") == {"const": "repair_proposal"}]
        return patch["properties"]["changes"]["items"]

    schema = response_schema("manifest", allow_patch=True)
    assert set(records(schema)["properties"]) == {"path", "value_json"}
    assert set(records(text_schema(schema))["properties"]) == {"path", "value_json", "value"}
    assert records(text_schema(schema))["required"] == ["path"]
    assert "value" not in PATCH_SCHEMA["properties"]["changes"]["items"]["properties"]


class RawReplies(Replies):
    async def __call__(self, messages):
        self.messages.append(copy.deepcopy(messages))
        value = next(self.values)
        return value if isinstance(value, str) else json.dumps(value)


@pytest.mark.asyncio
async def test_an_unparseable_correction_is_answered_by_asking_for_the_complete_declaration():
    from tests.unit.test_351_environment_discovery import PATH

    invalid = candidate()
    invalid["requirements"][0]["checks"] = ["missing"]
    broken = '{"tool":"repair_proposal","changes":[{"path":["manifest","checks",0,"argv"],"value_json":"[\\"sh\\",\\"-c'
    replies = RawReplies([{"tool": "read_file", "path": PATH}, {"manifest": invalid}, broken, {"manifest": candidate()},
                          {"tool": "read_file", "path": PATH}, assessment(candidate())])
    await run(replies, budget=AttemptBudget(max_schema_repairs=4))
    correction = json.loads(replies.messages[3][-1]["content"])
    assert "complete corrected declaration" in correction["validation_errors"][0]["guidance"]
    assert "repair_proposal" not in correction["instruction"].split("When supplied")[0]


@pytest.mark.asyncio
async def test_the_prompt_only_transport_describes_plain_values_to_the_model(monkeypatch):
    from coordinare.services.conducting import ModelReply, OpenAiApiBackend
    from coordinare.services.environment_response_schema import response_schema

    sent = []

    async def reply(messages, json_mode, *, response_schema=None, transport=None):
        sent.append(messages[0]["content"])
        return ModelReply(text='{"manifest": {}}')

    backend = OpenAiApiBackend(model="m", api_key="k", base_url="http://x/v1", structured_transport="json_object")
    monkeypatch.setattr(backend, "_chat_reply", reply)
    await backend.chat_json_with_metadata([{"role": "user", "content": "x"}],
                                          response_schema=response_schema("manifest", allow_patch=True))
    assert '"value": {"description"' in sent[0]


def test_a_digit_string_index_on_an_array_means_that_entry_and_nowhere_else():
    """v27: the author wrote "17" for checks[17]; both its value and value_json corrections were rejected."""
    from coordinare.services.environment_proposal_patch import apply_patch

    base = {"manifest": {"checks": [{"id": "a", "depends_on": ["x"]}], "env": {"0": "kept"}}}
    request = {"tool": "repair_proposal", "changes": [{"path": ["manifest", "checks", "0", "depends_on"], "value": []},
                                                      {"path": ["manifest", "env", "0"], "value": "set"}]}
    patched = apply_patch(base, request, max_bytes=4096)
    assert patched["manifest"]["checks"][0]["depends_on"] == []
    assert patched["manifest"]["env"] == {"0": "set"}
    with pytest.raises(EnvironmentDiscoveryError):
        apply_patch(base, {"tool": "repair_proposal", "changes": [{"path": ["manifest", "checks", "-1"], "value": {}}]},
                    max_bytes=4096)


def test_a_probe_without_its_fixed_schema_version_is_still_a_version_1_probe():
    from coordinare.services.environment_probes import ProbeRequest

    assert ProbeRequest.model_fields["schema_version"].default == 1
