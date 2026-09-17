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

import hashlib
import json
import re
import shlex
from pathlib import Path

import structlog

from performer.workflows.models import ExecutedCheck
from performer.workflows.qa.models import FlowStep, PlanCheck, TestPlan

log = structlog.get_logger(__name__)

#: Playwright driver template. Steps arrive as JSON so nothing the model wrote
#: is ever interpolated into executable code.
_FLOW_DRIVER = r"""
import json, re, sys
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
            elif action == 'http_assert':
                # Through the request context, not page.goto: an assertion is
                # not a navigation and must not open browser chrome.
                try:
                    spec = json.loads(value or '{}')
                except ValueError:
                    spec = None
                if not isinstance(spec, dict):
                    failures.append(
                        f"step {i}: http_assert value must be a JSON object of "
                        f"assertions (status, contains, json_path, equals), got {value!r}"
                    )
                else:
                    expected_status = spec.get('status')
                    contains = spec.get('contains')
                    # An empty contains asserts nothing: "" is in every
                    # response, including an HTTP 500's. Treat it as absent so
                    # the refusal below fires (411 round-seven review).
                    if isinstance(contains, str) and not contains.strip():
                        contains = None
                    path_expr = spec.get('json_path')
                    equals = spec.get('equals')
                    if (
                        expected_status is None
                        and contains is None
                        and not (path_expr and equals is not None)
                    ):
                        # An empty spec performs the request and asserts
                        # nothing: an HTTP 500 would pass (411 review).
                        failures.append(
                            f"step {i}: http_assert must assert at least one thing "
                            f"(status, contains, or json_path equals), got {value!r}"
                        )
                    else:
                        try:
                            response = page.request.get(target, timeout=15000)
                        except Exception as exc:
                            failures.append(f"step {i}: {type(exc).__name__}: {exc}")
                        else:
                            if expected_status is not None and response.status != expected_status:
                                failures.append(
                                    f"step {i}: expected status {expected_status}, "
                                    f"got {response.status} for {target}"
                                )
                            body = response.text()
                            if contains is not None and contains not in body:
                                failures.append(f"step {i}: response body does not contain {contains!r}")
                            if path_expr and equals is not None:
                                try:
                                    actual = body
                                    # Bracket indices are part of the JSONPath
                                    # grammar the PLAN persona advertises; a
                                    # dot-split alone turns $.items[0].id into
                                    # the literal key "items[0]" and fails every
                                    # array assertion (411 round-six review).
                                    normalized = re.sub(r"\[(\d+)\]", r".\1", str(path_expr).lstrip('$'))
                                    for part in normalized.split('.'):
                                        part = part.strip()
                                        if not part:
                                            continue
                                        decoded = json.loads(actual) if isinstance(actual, str) else actual
                                        actual = decoded[int(part) if part.isdigit() else part]
                                    if actual != equals:
                                        failures.append(
                                            f"step {i}: json_path {path_expr} was {actual!r}, "
                                            f"expected {equals!r}"
                                        )
                                except Exception as exc:
                                    failures.append(f"step {i}: json_path {path_expr} unusable: {exc}")
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
        check.command, cwd=cwd, plan_check_id=check.id,
    )


async def run_flow_check(toolkit, check: PlanCheck, *, cwd, driver_path: str) -> ExecutedCheck:
    """Drive a flow through the harness-owned Playwright script."""
    if check.kind == "visual" and not check.steps:
        # Fail closed (411 round-eight review): the driver on an empty step
        # list exits 0, which would satisfy the criterion while observing
        # nothing. prepare_visual_capture leaves a visual check with no steps
        # only when the plan has no navigation and no surface, so there is
        # nothing to screenshot — an unobservable check is a failed check,
        # not a passing one.
        return ExecutedCheck(
            plan_check_id=check.id,
            command=check.command or f"visual:{check.id}",
            exit_code=-1,
            output_excerpt=(
                "visual check is unobservable: the plan has no navigation and "
                "no declared surface, so there is nothing to screenshot"
            ),
            passed=False,
        )
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


def prepare_visual_capture(
    plan: TestPlan, out_dir: Path, base_url: str | None = None,
) -> None:
    """Give every visual check a navigation and a screenshot, in out_dir.

    The workflow declared visual_validation_required but captured nothing:
    report.py received visual_evidence=None from every caller, and the
    coordinare evidence floor then bounced every visual run (411 AC5). The
    capture is the workflow's own job, so the harness appends the step —
    which is also why a bare visual check is not a schema error.

    Two details the first review caught: the driver starts on about:blank, so
    the check navigates to the plan's first surface BEFORE the screenshot (a
    bare screenshot otherwise captures a blank page); and a screenshot step
    the model wrote itself is NORMALISED to the harness path, because
    collect_visual_evidence looks in exactly one place per check — a custom
    target would be captured but never collected.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for check in plan.checks:
        if check.kind != "visual":
            continue
        # The harness owns capture: EVERY model-supplied screenshot step is
        # replaced by one harness-owned capture after the navigation, so no
        # arbitrary target is left for Playwright to write outside the
        # evidence dir (411 round-four review) and no extra model screenshot
        # survives with one it controls (round-five review). The strip runs
        # BEFORE the no-navigation bail-out below: a check that will get no
        # capture step must not keep a model screenshot with an escape
        # target (411 round-eight review).
        for s in [s for s in check.steps if s.action == "screenshot"]:
            check.steps.remove(s)
        has_goto = any(s.action == "goto" for s in check.steps)
        if not has_goto and not plan.surfaces:
            # Nowhere to navigate: a screenshot here captures about:blank,
            # which is not evidence of the app (round-two review). Leave the
            # check with no capture step — collect then finds nothing and
            # the run must earn its screenshot from the fallback capture or
            # bounce, never from a blank page. run_flow_check refuses to
            # execute the now-empty check at all (round-eight review).
            continue
        target = str(out_dir / _evidence_name(check.id))
        check.steps.append(FlowStep(action="screenshot", target=target))
        if not has_goto:
            # The planner permits relative routes as surfaces and the
            # baseline resolves them, but this inserted goto is written by
            # the harness — a non-URL surface is not something Playwright
            # will accept as a target, so resolve it against the booted
            # origin exactly the way rewrite_targets normalises
            # model-supplied flow steps. That means bare relative routes
            # (signin) as well as root-relative ones (/route): resolve_target
            # supports both shapes, and passing the bare form through would
            # navigate nowhere (411 round-eight review).
            surface = plan.surfaces[0]
            if base_url and not surface.startswith("http"):
                surface = f"{base_url.rstrip('/')}/{surface.lstrip('/')}"
            check.steps.insert(0, FlowStep(action="goto", target=surface))


def rewrite_command_placeholders(plan: TestPlan, base_url: str | None) -> int:
    """Substitute $BASE_URL/$PORT in command checks with the booted origin.

    plan_needs_server treats the placeholders as app references and boots the
    app, but the placeholder itself was never resolved: `curl
    $BASE_URL/api/status` executed with an empty URL and failed its check
    despite a healthy boot (411 round-six review). The workflow command runs
    under a plain shell with no plan env, so the rewrite — not an env export —
    is the seam that reaches it. Returns the number of rewritten commands.
    """
    if not base_url:
        return 0
    origin = base_url.rstrip("/")
    port = origin.rsplit(":", 1)[-1]
    # The command runs under `bash -c`, so a config-derived value carrying
    # shell syntax (a PROTOCOL like `http; touch x`, already covered by the
    # boot tests) would become executable the moment it is interpolated
    # textually. shlex.quote is inert for the ordinary http://host:port
    # shape and neutralizes anything else; the lambdas keep re.sub from
    # reading the quoted text as a replacement template (411 round-eight
    # review).
    safe_origin = shlex.quote(origin)
    safe_port = shlex.quote(port)
    base_re = re.compile(r"\$\{?BASE_URL\}?")
    port_re = re.compile(r"\$\{?PORT\}?")
    # plan_needs_server also boots on a hard-coded loopback URL in the
    # command, but rewriting only the placeholders left `curl
    # http://localhost:3000/api/health` pointed at a port the booted app
    # does not serve — a connection failure reported as a code defect
    # (411 round-eight review). Normalize detected loopback origins onto
    # the booted one.
    local_url_re = re.compile(r"https?://(?:localhost|127\.0\.0\.1)(?::\d+)?")
    rewritten = 0
    for check in plan.checks:
        if check.kind != "command" or not check.command:
            continue
        # Order matters: the loopback pass runs FIRST, because the base pass
        # inserts a loopback origin of its own and a later local pass would
        # re-match the text it just inserted.
        new = local_url_re.sub(lambda m: safe_origin, check.command)
        new = base_re.sub(lambda m: safe_origin, new)
        if port.isdigit():
            new = port_re.sub(lambda m: safe_port, new)
        if new != check.command:
            check.command = new
            rewritten += 1
    return rewritten


def _evidence_name(check_id: str) -> str:
    """The evidence filename for a check id.

    The id is model-controlled but becomes a filesystem path, so it is
    slugified to a bounded [A-Za-z0-9_-] name: a path separator in an id can
    otherwise escape the evidence directory and make Playwright write (and
    collection read) outside it (411 round-four review). The slug is not
    injective — a/b and a_b would collide — so a digest of the raw id is
    appended, keeping distinct checks on distinct files (round-five review).
    """
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", str(check_id)).strip("_")[:60]
    digest = hashlib.sha256(str(check_id).encode("utf-8")).hexdigest()[:8]
    return f"qa_visual_{slug or 'check'}_{digest}.png"


def collect_visual_evidence(plan: TestPlan, out_dir: Path) -> list[dict]:
    """Evidence entries for the screenshots the capture steps produced.

    Only files that exist AND are non-empty are evidence — a capture that did
    not land must read as 'capture unavailable', not as a fabricated path, and
    a zero-byte file a failed or interrupted capture leaves behind must not
    satisfy the visual evidence floor (411 round-six review).
    """
    out_dir = Path(out_dir)
    evidence: list[dict] = []
    for check in plan.checks:
        if check.kind != "visual":
            continue
        path = out_dir / _evidence_name(check.id)
        if path.is_file() and path.stat().st_size > 0:
            evidence.append({
                "label": f"visual {check.id}",
                "kind": "screenshot",
                "path_or_url": str(path),
            })
    return evidence


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
                    await run_flow_check(toolkit, check, cwd=cwd, driver_path=driver_path),
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
                ),
            )
    return results
