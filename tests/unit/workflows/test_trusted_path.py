"""T017c — FR-004: workflows load ONLY from the trusted package directory.

The performer clones arbitrary repositories.  A workflow is operator-owned
code; a cloned repo must never be able to introduce or alter one (spec 144
trust boundary, spec 131 agent-config exclusion).
"""
from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "hostile_name",
    [
        "../evil",
        "../../evil",
        "..%2Fevil",
        "/etc/passwd",
        "workspace.evil",
        "performer.backends.claude_code",  # a real module, but not a workflow
        "os.path",
    ],
)
def test_names_resolving_outside_the_package_are_refused(hostile_name):
    from performer.workflows import UnsupportedWorkflowError, get_workflow
    from performer.workflows.base import UntrustedWorkflowPath

    with pytest.raises((UnsupportedWorkflowError, UntrustedWorkflowPath)):
        get_workflow(hostile_name)


def test_registry_targets_all_resolve_inside_the_trusted_root():
    """Every registered workflow module must live under the package dir.  This
    fails loudly if someone later registers a target outside the tree."""
    from importlib import import_module
    from pathlib import Path

    from performer.workflows import SUPPORTED_WORKFLOWS
    from performer.workflows.base import TRUSTED_WORKFLOW_ROOT

    for name, (module_name, _cls) in SUPPORTED_WORKFLOWS.items():
        module = import_module(module_name)
        module_path = Path(module.__file__).resolve()  # type: ignore[arg-type]
        assert module_path.is_relative_to(TRUSTED_WORKFLOW_ROOT), (
            f"workflow {name!r} resolves to {module_path}, outside "
            f"{TRUSTED_WORKFLOW_ROOT}"
        )


def test_a_workflow_file_dropped_into_a_clone_is_not_loadable(tmp_path, monkeypatch):
    """The concrete attack: a cloned repo ships its own workflow module and
    tries to get the performer to run it."""
    from performer.workflows import UnsupportedWorkflowError, get_workflow
    from performer.workflows.base import UntrustedWorkflowPath

    clone = tmp_path / "cloned_repo"
    clone.mkdir()
    (clone / "evil_workflow.py").write_text(
        "class EvilWorkflow:\n"
        "    name = 'evil'\n"
        "    async def run(self, stand, score, toolkit):\n"
        "        raise AssertionError('untrusted workflow executed')\n",
    )
    # Even with the clone on sys.path, the name must not resolve.
    monkeypatch.syspath_prepend(str(clone))
    with pytest.raises((UnsupportedWorkflowError, UntrustedWorkflowPath)):
        get_workflow("evil_workflow")
    with pytest.raises((UnsupportedWorkflowError, UntrustedWorkflowPath)):
        get_workflow("evil")


def test_a_registered_target_outside_the_package_is_still_refused(monkeypatch):
    """Defense in depth for FR-004.

    The allow-list already rejects unregistered names, so the path guard in
    get_workflow is only reachable when the REGISTRY itself is wrong — a bad
    future registration, or a compromised registry.  Mutation testing showed
    that without this case the guard could be deleted with every other trust
    test still green, i.e. it was untested code that merely looked protective.
    """
    from performer import workflows as wf
    from performer.workflows.base import UntrustedWorkflowPath

    # A real, importable module that lives OUTSIDE performer.workflows.
    monkeypatch.setitem(
        wf.SUPPORTED_WORKFLOWS,
        "smuggled",
        ("performer.backends.claude_code", "ClaudeCodeBackend"),
    )
    with pytest.raises(UntrustedWorkflowPath) as exc:
        wf.get_workflow("smuggled")
    assert "outside the trusted workflow root" in str(exc.value)
