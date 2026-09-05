"""Step 3: run the plan (spec 164).

The model decided WHAT to do; this owns HOW. That split is the lesson
``qa_capture.py`` already encodes: small models drive browsers badly -- they run
the capture under the env-cache's Playwright-less python3, or reach for Selenium
-- and a multi-step flow is strictly harder than the single screenshot that
already needed a deterministic backstop.

So a flow is a list of declarative steps from a closed action enum, executed by
code here. An action outside the enum is a schema violation caught upstream,
not an improvisation performed here.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

import structlog

from performer.workflows.models import ExecutedCheck
from performer.workflows.qa.models import FlowStep, PlanCheck, TestPlan

log = structlog.get_logger(__name__)

#: Playwright driver template. Steps arrive as JSON so nothing the model wrote
#: is ever interpolated into executable code.
_FLOW_DRIVER = r"""
import json, sys
from playwright.sync_api import sync_playwright

steps = json.loads(sys.argv[1])
failures = []
with sync_playwright() as p:
    browser = p.chromium.launch(args=['--no-sandbox', '--disable-dev-shm-usage'])
    page = browser.new_page()
    try:
        for i, s in enumerate(steps):
            action, target, value = s['action'], s.get('target'), s.get('value')
            if action == 'goto':
                page.goto(target, timeout=15000)
            elif action == 'fill':
                page.fill(target, value or '')
            elif action == 'click':
                page.click(target, timeout=10000)
            elif action == 'expect_text':
                if (value or '') not in page.content():
                    failures.append(f"step {i}: expected text {value!r} not found")
            elif action == 'screenshot':
                page.screenshot(path=target or '/tmp/qa_flow.png', full_page=True)
    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        browser.close()
print(json.dumps({'failures': failures}))
sys.exit(1 if failures else 0)
"""

#: The performer image's interpreter. Used EXPLICITLY because the env-cache
#: prepends its own Playwright-less python3 onto PATH, shadowing it -- the same
#: constraint qa_capture.py documents.
SYSTEM_PYTHON = "/usr/local/bin/python3"


def resolve_interpreter(*, exists=None) -> str:
    """Return the interpreter to run the flow driver with.

    In the performer, SYSTEM_PYTHON exists and wins. Off-image (the scenario
    eval runs on the host) it does not, and the driver failed with exit 127
    until this existed.

    The fallback is ``sys.executable``, never a bare ``python3``: resolving
    through PATH is precisely the shadowing bug the explicit path avoids, and
    reintroducing it inside the container would be worse than the crash.
    """
    import os
    import sys

    check = exists or os.path.exists
    return SYSTEM_PYTHON if check(SYSTEM_PYTHON) else sys.executable


async def run_command_check(toolkit, check: PlanCheck, *, cwd) -> ExecutedCheck:
    """Run a planned shell command.

    DESIGN DECISION, stated so a reviewer does not re-raise it as a bug:
    ``check.command`` is model output and is executed as a shell command,
    unescaped. That is the point of a command check -- the plan says "run the
    tests", and the tests are a shell command. It is exactly what every backend
    harness already does with model-chosen commands; the ephemeral container is
    the trust boundary, not string escaping. What IS escaped is anything the
    model wrote that reaches a shell *incidentally* (flow payloads, URLs).
    """
    return await toolkit.run_command(
        check.command or "true", cwd=cwd, plan_check_id=check.id
    )


async def run_flow_check(toolkit, check: PlanCheck, *, cwd, driver_path: str) -> ExecutedCheck:
    """Drive a flow through the harness-owned Playwright script."""
    ensure_flow_driver(Path(driver_path))
    payload = json.dumps([_step_dict(s) for s in check.steps])
    # The system interpreter explicitly: the env-cache prepends its own
    # Playwright-less python3 onto PATH, which shadows it.
    interpreter = resolve_interpreter()
    cmd = f"{interpreter} {shlex.quote(driver_path)} {shlex.quote(payload)}"
    return await toolkit.run_command(cmd, cwd=cwd, plan_check_id=check.id)


def _step_dict(step: FlowStep) -> dict:
    return {"action": step.action, "target": step.target, "value": step.value}


def flow_driver_source() -> str:
    return _FLOW_DRIVER


def ensure_flow_driver(path: Path) -> Path:
    """Write the driver to *path*, creating parents.

    Idempotent: a workspace may run several flow checks, and each must be able
    to assume the driver is present without coordinating with the others.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_FLOW_DRIVER)
    return path


async def run_execute_step(toolkit, plan: TestPlan, *, cwd, driver_path: str) -> list[ExecutedCheck]:
    """Run every planned check, recording a real result for each.

    A check that raises still produces an ExecutedCheck with a non-zero exit
    code: a missing result would leave its criterion unbound, which reads as
    "not demonstrated" rather than as an error, hiding the real cause.
    """
    results: list[ExecutedCheck] = []
    for check in plan.checks:
        try:
            if check.kind == "command":
                results.append(await run_command_check(toolkit, check, cwd=cwd))
            elif check.kind in ("flow", "visual"):
                results.append(
                    await run_flow_check(toolkit, check, cwd=cwd, driver_path=driver_path)
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("qa.execute.check_raised", check_id=check.id, error=str(exc))
            results.append(
                ExecutedCheck(
                    plan_check_id=check.id,
                    command=check.command or f"flow:{check.id}",
                    exit_code=-1,
                    output_excerpt=f"{type(exc).__name__}: {exc}",
                    passed=False,
                )
            )
    return results
