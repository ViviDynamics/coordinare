"""Workflow factory — maps configured workflow names to implementations.

Mirrors ``performer.backends``: a named, swappable strategy chosen by operator
config.  The registry is module-level on purpose so "is this a real workflow
name?" can be answered at config-load time without importing or instantiating
anything (the property spec 161 added to ``SUPPORTED_BACKENDS``).

FR-004: resolution is confined to this package.  Workflows are operator-owned;
a cloned repository must never be able to introduce or alter one, so a name is
only ever looked up in ``SUPPORTED_WORKFLOWS`` and the resolved module is
verified to live under ``TRUSTED_WORKFLOW_ROOT`` before use.
"""
from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

from performer.workflows.base import TRUSTED_WORKFLOW_ROOT, UntrustedWorkflowPath

if TYPE_CHECKING:
    from performer.workflows.base import RoleWorkflow


class UnsupportedWorkflowError(ValueError):
    """Raised when an unknown workflow name is requested."""


#: Supported workflow names mapped to ``(module, class)``.
#:
#: Only names in this dict are resolvable.  An arbitrary importable module name
#: is NOT a workflow, however well-formed it looks.
SUPPORTED_WORKFLOWS: dict[str, tuple[str, str]] = {
    "env_bootstrap": ("performer.workflows.env_bootstrap", "EnvBootstrapWorkflow"),
    "noop": ("performer.workflows.noop", "NoopWorkflow"),
    "qa": ("performer.workflows.qa", "QAWorkflow"),
    "architect": ("performer.workflows.architect", "ArchitectWorkflow"),
    "assessor": ("performer.workflows.assessor", "AssessorWorkflow"),
    "implementer": ("performer.workflows.implementer", "ImplementerWorkflow"),
    "reviewer": ("performer.workflows.reviewer", "ReviewerWorkflow"),
    "security": ("performer.workflows.security", "SecurityWorkflow"),
    "documenter": ("performer.workflows.documenter", "DocumenterWorkflow"),
    "closer": ("performer.workflows.closer", "CloserWorkflow"),
    "advocate": ("performer.workflows.advocate", "AdvocateWorkflow"),
    "curator": ("performer.workflows.curator", "CuratorWorkflow"),
}


def is_supported_workflow(name: str | None) -> bool:
    """True when *name* is a registered workflow.

    Cheap and side-effect free: imports nothing.  Coordinare's config validation
    uses this so a typo'd workflow name fails at load rather than mid-dispatch.
    """
    if not name or not isinstance(name, str):
        return False
    return name in SUPPORTED_WORKFLOWS


def get_workflow(name: str) -> "RoleWorkflow":
    """Return a new workflow instance for *name*.

    Raises ``UnsupportedWorkflowError`` when the name is not registered, and
    ``UntrustedWorkflowPath`` when a registered target somehow resolves outside
    this package (FR-004 — belt and braces against a bad future registration).
    """
    target = SUPPORTED_WORKFLOWS.get(name)
    supported = ", ".join(sorted(SUPPORTED_WORKFLOWS))
    if target is None:
        raise UnsupportedWorkflowError(
            f"unsupported workflow {name!r}; supported: {supported}",
        )

    module_name, class_name = target
    try:
        module = import_module(module_name)
    except ImportError as exc:  # pragma: no cover - registration error
        raise UnsupportedWorkflowError(
            f"workflow {name!r} could not be imported from {module_name!r}; "
            f"supported: {supported}",
        ) from exc

    module_file = getattr(module, "__file__", None)
    if not module_file or not Path(module_file).resolve().is_relative_to(
        TRUSTED_WORKFLOW_ROOT,
    ):
        raise UntrustedWorkflowPath(
            f"workflow {name!r} resolves to {module_file!r}, outside the trusted "
            f"workflow root {TRUSTED_WORKFLOW_ROOT}",
        )

    cls = getattr(module, class_name, None)
    if cls is None:  # pragma: no cover - registration error
        raise UnsupportedWorkflowError(
            f"workflow {name!r} names missing class {class_name!r}; "
            f"supported: {supported}",
        )
    return cls()
