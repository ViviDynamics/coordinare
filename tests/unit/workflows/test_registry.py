"""T006 — workflow registry (spec 164 FR-003, mirrors the backend registry)."""
from __future__ import annotations

import pytest


def test_supported_workflows_importable_without_instantiating():
    """Spec 161 made SUPPORTED_BACKENDS module-level so a typo is caught at
    config load rather than mid-run.  The workflow registry inherits that."""
    from performer.workflows import SUPPORTED_WORKFLOWS

    assert isinstance(SUPPORTED_WORKFLOWS, dict)
    assert SUPPORTED_WORKFLOWS, "registry must not be empty"
    for name, target in SUPPORTED_WORKFLOWS.items():
        assert isinstance(name, str) and name
        assert isinstance(target, tuple) and len(target) == 2


def test_get_workflow_returns_instance_for_known_name():
    from performer.workflows import SUPPORTED_WORKFLOWS, get_workflow
    from performer.workflows.base import RoleWorkflow

    name = next(iter(SUPPORTED_WORKFLOWS))
    wf = get_workflow(name)
    assert isinstance(wf, RoleWorkflow)
    assert wf.name == name


def test_get_workflow_raises_for_unknown_name():
    from performer.workflows import UnsupportedWorkflowError, get_workflow

    with pytest.raises(UnsupportedWorkflowError) as exc:
        get_workflow("does-not-exist")
    # The message must list what IS supported; a bare failure sends the reader
    # to the source to find out what they should have typed.
    assert "does-not-exist" in str(exc.value)
    assert "supported:" in str(exc.value)


def test_unknown_name_is_rejected_before_dispatch_not_during():
    """is_supported_workflow answers 'is this a real name?' without importing
    or instantiating anything, so config validation can call it cheaply."""
    from performer.workflows import SUPPORTED_WORKFLOWS, is_supported_workflow

    assert is_supported_workflow(next(iter(SUPPORTED_WORKFLOWS))) is True
    assert is_supported_workflow("nope") is False
    assert is_supported_workflow("") is False
    assert is_supported_workflow(None) is False  # type: ignore[arg-type]


def test_architect_is_a_registered_workflow_resolving_inside_the_trusted_root():
    """165: the second consumer of the layer resolves like the first."""
    from performer.workflows import get_workflow, is_supported_workflow

    assert is_supported_workflow("architect") is True
    wf = get_workflow("architect")
    assert wf.name == "architect"
    assert not is_supported_workflow("architec"), "a typo is still unknown"


def test_implementer_is_a_registered_workflow():
    """167: implementer workflow resolves and is registered."""
    from performer.workflows import get_workflow, is_supported_workflow

    assert is_supported_workflow("implementer") is True
    wf = get_workflow("implementer")
    assert wf.name == "implementer"
    assert not is_supported_workflow("implement"), "a typo is still unknown"
