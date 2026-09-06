"""RoleWorkflow protocol and its result/metric value objects (spec 164).

A role workflow runs an ordered sequence of small, individually verifiable
steps in place of the pre-164 "one backend invocation, then post-hoc parsing"
path.  It runs entirely inside the performer: nothing here calls coordinare
mid-run (FR-001).  The coordinare/performer wire contract is unchanged — a
workflow's output is turned into the same ``PerformerResponse`` the single
backend path produces.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from performer.models import BackendEvent, Score, Stand


@dataclass
class WorkflowMetrics:
    """Per-run counters, used for the spec's performance budgets."""

    model_calls: int = 0
    truncation_retries: int = 0
    schema_reprompts: int = 0
    commands_run: int = 0
    step_durations_ms: dict[str, int] = field(default_factory=dict)
    baseline_skipped: bool = False
    #: Which attempt at this card this run is (1 = first). The spec's success
    #: metric is rounds-to-green per ISSUE, so a single run can only contribute
    #: its own ordinal -- coordinare aggregates. Emitting it here is what makes
    #: the metric measurable rather than inferred from board history later.
    round_number: int = 1
    #: True when this run's verdict was a pass, so a consumer can find the
    #: round at which the card went green without re-reading the report.
    reached_green: bool = False
    #: The head app was already answering on PORT and was adopted rather than
    #: launched by this run. See AppBoot.adopted_existing_server.
    adopted_existing_server: bool = False


@dataclass
class WorkflowResult:
    """What a workflow hands back to the performer core."""

    #: The role's structured report — the same shape ``main.py`` already builds
    #: a ``PerformerResponse`` from.
    report: dict[str, Any] = field(default_factory=dict)
    #: Structured repair brief for the next stage.  Empty on success; absence is
    #: an empty list, never ``None`` (contracts/qa_findings.md).
    findings: list[dict[str, Any]] = field(default_factory=list)
    events: list["BackendEvent"] = field(default_factory=list)
    metrics: WorkflowMetrics = field(default_factory=WorkflowMetrics)


@runtime_checkable
class RoleWorkflow(Protocol):
    """Strategy interface implemented by each role workflow."""

    name: str

    async def run(
        self,
        stand: "Stand",
        score: "Score",
        toolkit: Any,
    ) -> WorkflowResult:
        """Execute every step in order and return the assembled result."""
        ...


class WorkflowError(RuntimeError):
    """Base class for workflow-layer failures."""


class ModelCallCeilingExceeded(WorkflowError):
    """Raised when a run exceeds its model-call budget (never silently continues)."""


class TruncatedResponse(WorkflowError):
    """Raised when a model response is cut short by ``finish_reason: length``.

    Distinct from a parse failure on purpose: coordinare #244 is the case where a
    truncation reaches the caller as empty content and is misread as malformed
    output.  Keeping the two exceptions separate keeps the causes separable.
    """


class SchemaViolation(WorkflowError):
    """Raised when a model response fails its step schema after one reprompt."""


class UntrustedWorkflowPath(WorkflowError):
    """Raised when a workflow resolves outside the trusted package directory.

    FR-004: workflows are operator-owned.  A cloned repository must never be
    able to introduce or alter one.
    """


#: The directory workflows may be loaded from.  Anything resolving outside this
#: tree is refused (FR-004).
TRUSTED_WORKFLOW_ROOT = Path(__file__).resolve().parent
