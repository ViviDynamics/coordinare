"""Step 1: derive what to check (spec 164).

This step has never existed. QA today reads a diff and starts doing whatever
looks checkable, which is why "checks what's easy" is one of the four named
failures. Producing the plan BEFORE anything executes means it cannot be
quietly retrofitted to whatever happened to pass, and it makes the plan itself
evidence: a criterion with no plan entry and no executed check cannot be
counted as passed.

Fails closed (R7): a plan with no checks is a step that could not run, not a
run with nothing to do. QA's historical failure is the confident pass on
unverified work, so the conservative direction is the correct one.
"""
from __future__ import annotations

from performer.workflows.base import WorkflowError
import structlog

from performer.workflows.budget import Budget
from performer.workflows.qa.models import TestPlan
from performer.workflows.qa.personas import PLAN


log = structlog.get_logger(__name__)


class EmptyPlan(WorkflowError):
    """Raised when planning produced no checks at all.

    Carries the criteria: with criteria present this is "not demonstrated" (a
    FAIL with one unmet finding each), and only with NO criteria is it a
    could-not-verify. Round-two review found the cosmetic_noop fixture could
    otherwise dodge a verdict by producing an empty plan.
    """

    def __init__(self, message: str, criteria: list[str] | None = None) -> None:
        super().__init__(message)
        self.criteria = list(criteria or [])


def effective_criteria(score) -> tuple[list[str], str]:
    """The criteria QA plans against, and where they came from.

    165 (FR-017): when the architect's verification brief is present its
    criteria are authoritative and already testable (surface, action, expected
    observation), so the planner is told they are fixed. Otherwise the card's
    acceptance criteria are used exactly as before 165.
    """
    brief = getattr(score, "verification_brief", None) or {}
    items = brief.get("criteria") if isinstance(brief, dict) else None
    if isinstance(items, list) and items:
        rendered = []
        for c in items:
            if not isinstance(c, dict):
                continue
            surface = str(c.get("surface", "")).strip()
            action = str(c.get("action", "")).strip()
            expected = str(c.get("expected", "")).strip()
            text = " ".join(part for part in (f"{surface}:" if surface else "", action, f"-> {expected}" if expected else "") if part)
            if text:
                rendered.append(text)
        if rendered:
            return rendered, "blueprint"
    return list(getattr(score, "acceptance_criteria", []) or []), "card"


def _prompt(
    criteria: list[str], diff: str, description: str, base_url: str | None, source: str = "card"
) -> list[dict]:
    criteria_block = "\n".join(f"- {c}" for c in criteria) or "(none stated)"
    if source == "blueprint":
        criteria_block += (
            "\n\nThese criteria are fixed by the architect's blueprint: plan checks "
            "for exactly these, do not add, merge or reinterpret them."
        )
    # Without this the model invents a conventional port. The first live eval
    # run planned `curl localhost:3000` against an app on an ephemeral port, and
    # the resulting failure demoted a criterion the flow check had demonstrated.
    where = (
        f"The application under test is served at {base_url}. Use this exact "
        "origin for every URL, including in shell commands. Do NOT assume a "
        "conventional port."
        if base_url
        else "The application's URL is not known, and no server is confirmed "
        "running. Prefer command checks; do not invent a host or port."
    )
    return [
        {
            "type": "text",
            "text": (
                f"{where}\n\n"
                f"Acceptance criteria:\n{criteria_block}\n\n"
                f"Card description:\n{description or '(none)'}\n\n"
                f"Pull request diff:\n{diff or '(diff unavailable)'}"
            ),
        }
    ]


async def run_plan_step(toolkit, score, *, base_url: str | None = None) -> TestPlan:
    """Produce the test plan, or fail closed."""
    criteria, source = effective_criteria(score)
    log.info("qa.plan.criteria_source", source=source, count=len(criteria))
    plan = await toolkit.call_model(
        persona=PLAN,
        schema=TestPlan,
        content=_prompt(
            criteria,
            getattr(score, "pr_diff", "") or "",
            getattr(score, "description", "") or "",
            base_url,
            source,
        ),
        budget=Budget.for_step("plan"),
    )

    if not plan.checks:
        raise EmptyPlan(
            "planning produced no checks; failing closed rather than reporting "
            "a run with nothing to verify",
            criteria,
        )

    # Drop checks that serve no stated criterion: they are noise, and counting
    # them later would let unbound work look like evidence.
    if criteria:
        stated = set(criteria)
        plan = TestPlan(
            checks=[c for c in plan.checks if c.criterion in stated],
            surfaces=plan.surfaces,
        )
        if not plan.checks:
            raise EmptyPlan(
                "planning produced no checks bound to a stated acceptance criterion",
                criteria,
            )
    return plan
