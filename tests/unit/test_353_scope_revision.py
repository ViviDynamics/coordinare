"""Strict bounded revisions retain daemon-owned source obligations atomically."""

from __future__ import annotations

import copy

import pytest
from coordinare_service_inference.environment.scope import (
    ScopeCriterion,
    ScopeDefinition,
    criterion_id,
)
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from tests.unit.test_353_scope_contract import criterion


def revision_api():
    from coordinare_service_inference.environment.scope_revision import (
        ScopeRevision,
        apply_scope_revision,
    )

    return ScopeRevision, apply_scope_revision


def definition(*criteria):
    from coordinare_service_inference.environment.scope import ScopeCriterionProposal

    proposals = [ScopeCriterionProposal.model_validate(c) for c in criteria or [criterion()]]
    return ScopeDefinition(
        source_commit="a" * 40,
        inventory_digest="b" * 64,
        inputs={"guide": "c" * 64},
        criteria=[ScopeCriterion(**c.model_dump(), id=criterion_id(c)) for c in proposals],
    )


def delta(base, *, add=None, reassess=None):
    return {"base_digest": base.digest(), "add": add or [], "reassess": reassess or []}


def apply(base, value, inputs=None):
    model, merge = revision_api()
    return merge(base, model.model_validate(value), inspected_inputs=inputs or dict(base.inputs))


def test_add_and_empty_revision_preserve_required_definition_and_input_history():
    base = definition()
    before = base.model_dump()
    result = apply(
        base,
        delta(base, add=[criterion("nested", usability="A request is persisted")]),
        {**base.inputs, "nested": "d" * 64},
    )
    assert len(result.criteria) == 2
    assert result.criteria[0].model_dump() == before["criteria"][0]
    assert (
        result.source_commit == base.source_commit
        and result.inventory_digest == base.inventory_digest
    )
    assert result.inputs == {**base.inputs, "nested": "d" * 64}
    assert base.model_dump() == before
    assert apply(base, delta(base)) == base
    assert apply(base, delta(base)) is not base


@pytest.mark.parametrize("applicability", ["unknown", "not_applicable"])
def test_nonrequired_reassessment_recomputes_identity(applicability):
    base = definition(criterion(applicability=applicability))
    result = apply(
        base,
        delta(base, reassess=[{"criterion_id": base.criteria[0].id, "criterion": criterion()}]),
    )
    assert result.criteria[0].id == definition().criteria[0].id
    assert result.criteria[0].id != base.criteria[0].id
    assert result.criteria[0].applicability == "required"


@pytest.mark.parametrize(
    "case",
    [
        "stale",
        "unknown",
        "repeat",
        "required",
        "duplicate",
        "rationale_duplicate",
        "retained_collision",
        "replacement_collision",
        "seventeen",
        "unread",
        "changed",
        "missing",
        "extra",
        "missing_field",
        "late_invalid",
    ],
)
def test_invalid_revision_is_atomic(case):
    base = definition(
        criterion(), criterion(applicability="unknown", usability="Unknown operation"),
    )
    value = delta(base)
    inputs = dict(base.inputs)
    replacement = {
        "criterion_id": base.criteria[1].id,
        "criterion": criterion(usability="Resolved operation"),
    }
    fresh = criterion(usability="New operation")
    if case == "stale":
        value["base_digest"] = "0" * 64
    elif case == "unknown":
        value["reassess"] = [{**replacement, "criterion_id": "S" + "0" * 16}]
    elif case == "repeat":
        value["reassess"] = [replacement, replacement]
    elif case == "required":
        value["reassess"] = [{**replacement, "criterion_id": base.criteria[0].id}]
    elif case == "duplicate":
        value["add"] = [fresh, fresh]
    elif case == "rationale_duplicate":
        value["add"] = [fresh, {**fresh, "rationale": "Another explanation"}]
    elif case == "retained_collision":
        value["add"] = [criterion()]
    elif case == "replacement_collision":
        value["reassess"] = [{**replacement, "criterion": criterion()}]
    elif case == "seventeen":
        value["add"] = [criterion(usability=f"operation {i}") for i in range(17)]
    elif case == "unread":
        value["add"] = [criterion("listed-only")]
    elif case == "changed":
        inputs["guide"] = "d" * 64
    elif case == "missing":
        inputs = {"new": "d" * 64}
    elif case == "extra":
        value["delete"] = [base.criteria[0].id]
    elif case == "missing_field":
        del value["reassess"]
    elif case == "late_invalid":
        value["add"] = [fresh]
        value["reassess"] = [{**replacement, "criterion_id": base.criteria[0].id}]
    before, wire = base.model_dump(), copy.deepcopy(value)
    with pytest.raises((ValidationError, PydanticCustomError)):
        apply(base, value, inputs)
    assert base.model_dump() == before and value == wire


def test_combined_sixteen_bound_and_final_criterion_limit():
    base = definition(criterion(applicability="unknown"))
    value = delta(
        base,
        add=[criterion(usability=f"operation {i}") for i in range(15)],
        reassess=[{"criterion_id": base.criteria[0].id, "criterion": criterion()}],
    )
    assert len(apply(base, value).criteria) == 16
    value["add"].append(criterion(usability="sixteenth addition"))
    with pytest.raises(ValidationError):
        apply(base, value)
    full = definition(*[criterion(usability=f"operation {i}") for i in range(512)])
    with pytest.raises(ValidationError):
        apply(full, delta(full, add=[criterion(usability="513")]))


def test_final_definition_byte_bound_is_preserved():
    base = definition(
        *[criterion(usability=f"operation {i}", applicability="unknown") for i in range(100)],
    )
    value = delta(
        base,
        add=[
            {
                **criterion(usability=f"large {i}"),
                "runtime_needs": ["x" * 2048 + str(j) for j in range(31)],
            }
            for i in range(16)
        ],
    )
    # Keep individual strings legal while exceeding the complete 1 MiB bound.
    for item in value["add"]:
        item["runtime_needs"] = ["x" * 2046 + str(j).zfill(2) for j in range(32)]
    with pytest.raises(ValidationError):
        apply(base, value)


def test_revision_envelope_is_required_strict_and_rejects_model_ids():
    from coordinare_service_inference.environment.scope_revision import ScopeRevisionEnvelope

    base = definition()
    valid = {"scope_revision": delta(base), "finding_responses": []}
    assert ScopeRevisionEnvelope.model_validate(valid).scope_revision.add == []
    for invalid in (
        {"scope_revision": delta(base)},
        {**valid, "scope_definition": {}},
        {**valid, "scope_revision": delta(base, add=[{**criterion(), "id": base.criteria[0].id}])},
    ):
        with pytest.raises(ValidationError):
            ScopeRevisionEnvelope.model_validate(invalid)


def test_only_a_required_criterion_released_as_remote_state_can_leave_the_required_set():
    from coordinare.services.environment_scope import preserve_scope, released_criteria

    model, merge = revision_api()
    base = definition({**criterion(), "description": "Branch protection is verifiable through an authorized gh session"})
    target = base.criteria[0]
    remote = {**target.model_dump(exclude={"id"}), "applicability": "not_applicable",
              "rationale": "Branch protection is GitHub repository state, outside the environment."}
    revision = model.model_validate(delta(base, reassess=[{"criterion_id": target.id, "criterion": remote}]))
    with pytest.raises(PydanticCustomError):
        merge(base, revision, inspected_inputs=dict(base.inputs))
    released = merge(base, revision, inspected_inputs=dict(base.inputs), releasable={target.id})
    assert released.criteria[0].applicability == "not_applicable"
    still_required = model.model_validate(delta(base, reassess=[{"criterion_id": target.id, "criterion": {
        **remote, "applicability": "required"}}]))
    with pytest.raises(PydanticCustomError):
        merge(base, still_required, inspected_inputs=dict(base.inputs), releasable={target.id})
    with pytest.raises(Exception, match="scope_obligation_removed"):
        preserve_scope(base, released)
    preserve_scope(base, released, released={target.id})
    from types import SimpleNamespace

    def record(category, affected):
        return SimpleNamespace(proposal=SimpleNamespace(category=category, affected_ids=affected))

    ledger = SimpleNamespace(records=[record("remote_state", [target.id]), record("coverage", ["S0000000000000000"])])
    assert released_criteria(ledger) == {target.id} and released_criteria(None) == set()


def test_a_criterion_needing_the_network_on_every_run_is_released_like_remote_state():
    """Maintainer decision 2026-10-24: SampleApp's dev compose stack runs apt-get on every start; v38's reviewers kept
    finding residual network fetches in offline workarounds for it."""
    from types import SimpleNamespace

    from coordinare_service_inference.environment.findings import FindingProposal

    from coordinare.services.environment_scope import (
        GRANULARITY_GUIDANCE,
        REVIEW_INSTRUCTIONS,
        RUNTIME_NETWORK_RULE,
        released_criteria,
    )

    def record(category, affected):
        return SimpleNamespace(proposal=SimpleNamespace(category=category, affected_ids=affected))

    ledger = SimpleNamespace(records=[record("runtime_network", ["S0000000000000001"]),
                                      record("remote_state", ["S0000000000000002"]),
                                      record("execution_definition", ["S0000000000000003"])])
    assert released_criteria(ledger) == {"S0000000000000001", "S0000000000000002"}
    assert RUNTIME_NETWORK_RULE in REVIEW_INSTRUCTIONS and RUNTIME_NETWORK_RULE in GRANULARITY_GUIDANCE
    assert "installing the tools it uses" in RUNTIME_NETWORK_RULE
    assert "runtime_network" in FindingProposal.model_json_schema()["properties"]["category"]["enum"]


def test_building_or_running_the_apps_own_deployment_images_is_released_like_remote_state():
    """Maintainer decision 2026-10-27: building SampleApp's own production and QA images under the validation runtime's
    emulation exceeded an hour on every diagnostic; agents need the development and test environment, not deployment
    images or stacks."""
    from types import SimpleNamespace

    from coordinare_service_inference.environment.findings import FindingProposal
    from coordinare_service_inference.environment.scope import SourceGapKind

    from coordinare.services.environment_scope import (
        DEPLOYMENT_IMAGE_RULE,
        GRANULARITY_GUIDANCE,
        RELEASE_CATEGORIES,
        REVIEW_INSTRUCTIONS,
        REVISION_INSTRUCTIONS,
        released_criteria,
    )

    def record(category, affected):
        return SimpleNamespace(proposal=SimpleNamespace(category=category, affected_ids=affected))

    ledger = SimpleNamespace(records=[record("deployment_image", ["S0000000000000004"]),
                                      record("execution_definition", ["S0000000000000005"])])
    assert released_criteria(ledger) == {"S0000000000000004"}
    assert "deployment_image" in RELEASE_CATEGORIES
    assert DEPLOYMENT_IMAGE_RULE in REVIEW_INSTRUCTIONS and DEPLOYMENT_IMAGE_RULE in GRANULARITY_GUIDANCE
    assert "deployment_image" in REVISION_INSTRUCTIONS
    assert "third-party service images" in DEPLOYMENT_IMAGE_RULE and "stay required" in DEPLOYMENT_IMAGE_RULE
    assert "documented development container or compose environment" in DEPLOYMENT_IMAGE_RULE
    assert "a test suite itself performs" in DEPLOYMENT_IMAGE_RULE
    assert "deployment_image" in FindingProposal.model_json_schema()["properties"]["category"]["enum"]
    assert "deployment_image" in SourceGapKind.__args__
