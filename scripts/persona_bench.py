#!/usr/bin/env python3
"""Persona-capability benchmark (077): role x backend x model, graded.

For every lifecycle role (assessor, architect, implementer, reviewer, security,
qa, tech_writer, closer, env_bootstrap) this dispatches a *deliberately
difficult* task with a known-correct answer or a planted issue to a backend on a
given model (reusing the proven performer ``/jobs`` dispatch), then grades the
result and categorises each cell into one of:

  PASS          — met the role contract AND the behavioural bar
                  (e.g. reviewer caught the planted bug)
  FAIL_MODEL    — ran fine but got it wrong (approved a buggy PR, architect
                  implemented instead of planning, qa rubber-stamped)
  FAIL_HARNESS  — fixable plumbing (dispatch_error, empty output, parse failure
                  on valid output, secret_missing, "does not support thinking")
  ERROR         — crash / timeout / never-ready

That harness-vs-model split is the point: it routes each failure to *us* (fix
the plumbing) or to *model selection* (don't use that model for that role).

Grading is **deterministic + LLM-judge**: deterministic checks read the role
contract artifact (status, JSON keys, workspace files, planted-marker keywords);
the judge (an LLM over the LiteLLM proxy) rules on behavioural correctness using
a per-role rubric and the known ground truth. A cell is PASS only when both
agree.

Fixtures live in the ``conductor-bench`` repo (see that repo's BENCH.md). The
clone URL and per-role PR URLs come from ``--repo`` and ``--prs <json>`` (the
JSON is produced by ``scripts/bench_setup.sh``).

Usage:
  scripts/persona_bench.py --repo <git_url> --prs tmp/bench_prs.json \\
      [--config config.yaml] [--roles reviewer,security,qa] \\
      [--backends codex-ephemeral,...] [--judge-model spark/gpt-oss:120b]
"""
from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import yaml

if TYPE_CHECKING:
    from collections.abc import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_backends import (
    IMAGE,
    READY_TIMEOUT_S,
    REPO_ROOT,
    _backend_model,
    _expand,
    _free_port,
    _gh_token,
    _load_env,
)

JOB_TIMEOUT_S = 900  # roles on slow reasoning models can take many minutes

# Signatures in a failure reason/summary that mean "our plumbing", not "the
# model can't do it". Keep in sync with the coordinare's transient-error markers.
HARNESS_MARKERS = (
    "dispatch_error", "secret_missing", "does not support thinking",
    "server disconnected", "readiness timeout", "container start failed",
    "connection reset", "connection refused", "transport error", "unreachable",
    "subprocess_exit", "no status response", "internal_error", "cannot write to",
    "WorkspaceSetupError", "auth_failed", "BACKEND_FORMAT_ERROR", "empty output",
)


# ---------------------------------------------------------------------------
# Grading context + verdict
# ---------------------------------------------------------------------------
@dataclass
class GradeCtx:
    role: str
    state: str | None              # job state: succeeded/failed/cancelled/None
    status: str                    # PerformerResponse.status (e.g. qa_passed)
    summary: str                   # full result.summary JSON text
    parsed: dict                   # parsed summary (best-effort)
    error_code: str | None
    cid: str | None                # container id (alive — docker exec OK)
    repo_subdir: str               # workspace path inside container (best-effort)


@dataclass
class Verdict:
    category: str = "ERROR"        # PASS | FAIL_MODEL | FAIL_HARNESS | ERROR
    contract_ok: bool = False      # produced the role's required artifact
    markers: list[str] = field(default_factory=list)  # planted-issue evidence found
    judge_correct: bool | None = None
    judge_quality: int | None = None
    detail: str = ""


def _docker_exec(cid: str, sh: str, timeout: int = 25) -> str:
    if not cid:
        return ""
    try:
        r = subprocess.run(["docker", "exec", cid, "sh", "-c", sh],
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _has_marker(text: str, *needles: str) -> list[str]:
    low = text.lower()
    return [n for n in needles if n.lower() in low]


def categorize(state: str | None, blob: str, contract_ok: bool, markers: list[str],
               judge_correct: bool | None) -> tuple[str, list[str]]:
    """Pure categorisation: route a graded cell to PASS / FAIL_MODEL /
    FAIL_HARNESS / ERROR. Returns (category, harness_markers_hit).

    Precedence:
      * a harness signature + (job didn't terminally succeed OR the contract
        artifact is absent) → FAIL_HARNESS (our plumbing, fixable)
      * terminal failure with no contract artifact + no harness signature → ERROR
      * otherwise judge behavioural correctness (judge authoritative; else
        deterministic contract+markers) → PASS / FAIL_MODEL
    """
    harness_hit = _has_marker(blob, *HARNESS_MARKERS)
    failed = state in ("failed", "cancelled")
    if harness_hit and (failed or not contract_ok):
        return "FAIL_HARNESS", harness_hit
    if failed and not contract_ok:
        return "ERROR", harness_hit
    correct = judge_correct if judge_correct is not None else (contract_ok and bool(markers))
    return ("PASS" if (contract_ok and correct) else "FAIL_MODEL"), harness_hit


# ---------------------------------------------------------------------------
# Deterministic graders — one per role.
# Each returns (contract_ok, markers, detail). The judge rules on correctness;
# these provide the structural signal + corroborating planted-marker evidence.
# ---------------------------------------------------------------------------
def grade_assessor(c: GradeCtx) -> tuple[bool, list[str], str]:
    # Task is under-specified → correct answer is "insufficient, ask questions".
    questions = c.parsed.get("questions") or []
    sufficient = None
    for k in ("sufficient",):
        if k in c.parsed:
            sufficient = c.parsed[k]
    contract_ok = c.status in ("assessment_complete", "blocked") or bool(questions)
    mk = []
    if questions:
        mk.append(f"{len(questions)}_questions")
    if sufficient is False:
        mk.append("flagged_insufficient")
    return contract_ok, mk, f"questions={len(questions)} sufficient={sufficient}"


def grade_architect(c: GradeCtx) -> tuple[bool, list[str], str]:
    # Must PLAN, not implement: a plan file should exist and src/ must be
    # unchanged (the classic weak-model trap is to start coding).
    plan = _docker_exec(c.cid, f"find {c.repo_subdir} -iname 'plan*.md' 2>/dev/null | head -1")
    src_changed = _docker_exec(
        c.cid,
        f"cd {c.repo_subdir} 2>/dev/null && git diff --name-only HEAD~1 2>/dev/null | grep -c '^src/' || echo 0",
    )
    implemented = src_changed.isdigit() and int(src_changed) > 0
    contract_ok = bool(plan) and not implemented
    mk = []
    if plan:
        mk.append("plan_file")
    if implemented:
        mk.append("CONTRACT_VIOLATION_implemented")
    return contract_ok, mk, f"plan={'y' if plan else 'n'} src_files_changed={src_changed}"


def grade_implementer(c: GradeCtx) -> tuple[bool, list[str], str]:
    # Branch ships a failing test for an unimplemented function; correct =
    # make the suite pass. Run pytest in the workspace before teardown.
    out = _docker_exec(
        c.cid,
        f"cd {c.repo_subdir} 2>/dev/null && (python -m pytest -q 2>&1 | tail -3) || echo NOPYTEST",
        timeout=90,
    )
    passed = "passed" in out and "failed" not in out and "error" not in out.lower()
    return passed, (["tests_pass"] if passed else []), f"pytest_tail={out[-120:]}"


def grade_reviewer(c: GradeCtx) -> tuple[bool, list[str], str]:
    # PR has a planted off-by-one that skips the last item → must request changes
    # and reference the bug.
    text = c.summary
    mk = _has_marker(text, "off-by-one", "off by one", "last item", "range(len",
                     "skips", "len(items) - 1", "boundary")
    contract_ok = c.status == "changes_requested"
    if c.status == "approved":
        mk.append("APPROVED_BUGGY_PR")
    return contract_ok, mk, f"status={c.status}"


def grade_security(c: GradeCtx) -> tuple[bool, list[str], str]:
    text = c.summary
    mk = _has_marker(text, "sql injection", "injection", "sqli", "eval(",
                     "eval ", "f-string", "parameteri", "untrusted", "rce")
    contract_ok = c.status == "security_failed"
    if c.status == "security_passed":
        mk.append("PASSED_VULN_PR")
    return contract_ok, mk, f"status={c.status}"


def grade_qa(c: GradeCtx) -> tuple[bool, list[str], str]:
    # Feature has a behaviour bug (KeyError when 'discount' absent). Correct QA
    # finds it AND shows it actually executed (non-empty verification_steps).
    report = c.parsed.get("report") or {}
    steps = report.get("verification_steps") or c.parsed.get("verification_steps") or []
    found_bug = bool(_has_marker(c.summary, "keyerror", "discount", "missing key",
                                 "raises", "empty cart", "crash"))
    contract_ok = c.status == "qa_failed" or (found_bug and bool(steps))
    mk = []
    if steps:
        mk.append(f"{len(steps)}_steps")
    if found_bug:
        mk.append("found_behavior_bug")
    if c.status == "qa_passed" and not found_bug:
        mk.append("RUBBER_STAMP")
    return contract_ok, mk, f"status={c.status} steps={len(steps)} found_bug={found_bug}"


def grade_tech_writer(c: GradeCtx) -> tuple[bool, list[str], str]:
    # New public api (apply_coupon) shipped with no docs → must add docs.
    doc = _docker_exec(
        c.cid,
        f"cd {c.repo_subdir} 2>/dev/null && (grep -rils 'coupon' README.md docs/ src/ 2>/dev/null | head -3)",
    )
    contract_ok = c.status == "docs_committed" and bool(doc)
    return contract_ok, (["docs_reference_coupon"] if doc else []), f"status={c.status} doc_hits={bool(doc)}"


def grade_closer(c: GradeCtx) -> tuple[bool, list[str], str]:
    # Clean, correct PR → closer should reach a terminal review verdict.
    contract_ok = c.status in ("approved", "changes_requested", "qa_passed")
    return contract_ok, ([c.status] if contract_ok else []), f"status={c.status}"


def grade_env_bootstrap(c: GradeCtx) -> tuple[bool, list[str], str]:
    # Bootstrap must produce a runnable env: confirm pytest is importable in
    # the cache after the install turn.
    contract_ok = c.status == "env_bootstrap_complete"
    py = _docker_exec(c.cid, f"cd {c.repo_subdir} 2>/dev/null && python -c 'import pytest' 2>&1 && echo OK || echo NO")
    if "OK" in py:
        return contract_ok, ["pytest_importable"], f"status={c.status} pytest=OK"
    return contract_ok, [], f"status={c.status} pytest=missing"


# ---------------------------------------------------------------------------
# Role task fixtures.
# ---------------------------------------------------------------------------
@dataclass
class RoleTask:
    label: str
    role: str               # canonical performer role string
    branch: str             # fixture branch (or "" → fresh synthetic branch)
    pr_key: str | None      # key into the --prs json for pr_url (review roles)
    title: str
    description: str
    acceptance: list[str]
    persona: str
    grader: Callable[[GradeCtx], tuple[bool, list[str], str]]
    rubric: str             # what "correct" means, for the LLM judge


TASKS: list[RoleTask] = [
    RoleTask(
        "assessor", "assessing", "", None,
        "Add a customer loyalty program",
        "We want to add a loyalty/rewards program to the cart. Make it work.",
        ["Loyalty program is implemented"],
        "You are the assessor. Decide whether this card is specified well enough "
        "to architect+implement. The request is deliberately vague.",
        grade_assessor,
        "The request is severely under-specified (no rules for earning/spending "
        "points, tiers, expiry, etc.). CORRECT = judge it INSUFFICIENT and ask "
        "specific clarifying questions. WRONG = declare it sufficient / proceed.",
    ),
    RoleTask(
        "architect", "architecting", "", None,
        "Design coupon-stacking rules",
        "Plan how multiple coupons could stack on one cart (precedence, caps). "
        "Produce an architecture plan only — do NOT write the implementation.",
        ["A plan document describing the approach"],
        "You are the architect. Produce a plan file (plan.md). Do NOT modify "
        "source code or implement the feature — planning only.",
        grade_architect,
        "CORRECT = a written plan, with NO source-code implementation. WRONG = "
        "started editing/implementing source files instead of planning.",
    ),
    RoleTask(
        "implementer", "implementing", "feat/impl-failing-test", None,
        "Implement shipping_cost to pass its tests",
        "tests/test_cart.py has a failing spec for bench.cart.shipping_cost: "
        "free under 1kg, flat $5 for 1-5kg, $10 above 5kg. Implement it.",
        ["pytest passes"],
        "You are the implementer. Make the failing test pass by implementing "
        "the function as specified. Keep changes minimal.",
        grade_implementer,
        "CORRECT = shipping_cost implemented so the full pytest suite passes. "
        "WRONG = tests still fail, or the function is wrong/missing.",
    ),
    RoleTask(
        "reviewer", "reviewing", "fix/reviewer-offbyone", "reviewer",
        "Review: discount_all",
        "Review the PR that adds discount_all (apply a percent discount to "
        "EVERY cart item).",
        ["Correctness reviewed"],
        "You are the reviewer. Give a binary verdict (approve / request changes) "
        "with specific, actionable comments on any correctness defects.",
        grade_reviewer,
        "The PR has a planted off-by-one (`range(len(items) - 1)`) that SKIPS the "
        "last item, contradicting the docstring ('every item'). CORRECT = request "
        "changes and identify that the last item is skipped. WRONG = approve.",
    ),
    RoleTask(
        "security", "security", "feat/security-sqli", "security",
        "Security review: user lookup + filter",
        "Security-review the PR adding lookup_user and compute_filter.",
        ["No injection vulnerabilities"],
        "You are the security reviewer. Flag any vulnerabilities with severity "
        "and location.",
        grade_security,
        "The PR has TWO planted vulns: SQL injection via f-string in lookup_user, "
        "and eval() of attacker-controlled request data in compute_filter. CORRECT "
        "= FAIL the review and flag at least the injection/eval. WRONG = pass it.",
    ),
    RoleTask(
        "qa", "qa", "feat/qa-behavior", "qa",
        "QA: checkout_total",
        "QA the PR adding checkout_total(cart).",
        ["checkout_total behaves correctly for typical carts"],
        "You are QA. Actually exercise the change (run it / write a quick check). "
        "Report pass/fail with concrete verification steps.",
        grade_qa,
        "checkout_total does `cart['discount']` and raises KeyError when 'discount' "
        "is absent (the common case). CORRECT = QA actually runs it, hits the "
        "failure, and FAILS qa with verification steps. WRONG = qa_passed with no "
        "real execution (rubber-stamp).",
    ),
    RoleTask(
        "tech_writer", "documenting", "feat/techwriter-nodoc", None,
        "Document coupon support",
        "The PR added apply_coupon (SAVE10 / SAVE25) with no documentation. "
        "Document the new public API.",
        ["apply_coupon is documented"],
        "You are the tech writer. Add documentation (docstring and/or README) "
        "for the new coupon API and commit it.",
        grade_tech_writer,
        "CORRECT = adds real documentation for apply_coupon (docstring/README "
        "mentioning the coupon codes). WRONG = no docs added.",
    ),
    RoleTask(
        "closer", "closing_review", "feat/closer-ready", "closer",
        "Closing review: line_count",
        "Final review of a small, correct PR adding line_count.",
        ["Ready to merge"],
        "You are the closer. Do a final review and give a binary verdict.",
        grade_closer,
        "This PR is clean and correct. CORRECT = a coherent terminal verdict "
        "(approve, or request changes with a real reason). WRONG = crash / no "
        "verdict / incoherent output.",
    ),
    RoleTask(
        "env_bootstrap", "env_bootstrap", "", None,
        "Bootstrap the dev environment",
        "Set up the dev environment per the README (Python 3.12, pip install "
        "-e .[test], pytest runnable).",
        ["pytest is runnable in the environment"],
        "You are the environment bootstrapper. Install the project's documented "
        "dependencies into the mounted env-cache so tests can run.",
        grade_env_bootstrap,
        "CORRECT = the environment ends up with pytest installed/runnable. WRONG "
        "= bootstrap reports done but pytest is not importable.",
    ),
]


# ---------------------------------------------------------------------------
# LLM judge (over the LiteLLM proxy).
# ---------------------------------------------------------------------------
def judge(task: RoleTask, ctx: GradeCtx, det_detail: str, base_url: str,
          api_key: str, model: str) -> tuple[bool | None, int | None, str]:
    """Ask an LLM whether the model's output is behaviourally CORRECT for the
    role, given the known ground truth. Returns (correct, quality0_5, reason)."""
    prompt = (
        "You are grading whether an AI agent correctly performed a software "
        f"role. ROLE: {task.label}. GROUND TRUTH / RUBRIC:\n{task.rubric}\n\n"
        f"Deterministic signals already gathered: {det_detail}\n\n"
        f"The agent's terminal output (truncated):\n{ctx.summary[:2500]}\n\n"
        "Reply with ONLY a JSON object: {\"correct\": true|false, "
        "\"quality\": 0-5, \"reason\": \"one sentence\"}."
    )
    try:
        r = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0, "max_tokens": 300},
            timeout=120,
        )
        content = r.json()["choices"][0]["message"]["content"]
        s = content[content.find("{"): content.rfind("}") + 1]
        d = json.loads(s)
        return bool(d.get("correct")), d.get("quality"), str(d.get("reason", ""))[:160]
    except Exception as exc:
        return None, None, f"judge_error: {type(exc).__name__}: {exc}"[:160]


# ---------------------------------------------------------------------------
# Dispatch one (task, endpoint) and grade it.
# ---------------------------------------------------------------------------
def run_cell(task: RoleTask, endpoint: dict, cfg: dict, dotenv: dict,
             repo_url: str, prs: dict, judge_cfg: dict | None, outdir: Path) -> dict:
    eid = endpoint["id"]
    backend = (endpoint.get("env", {}) or {}).get("BACKEND", "")
    model = _backend_model(cfg, endpoint)
    res = {"role": task.label, "endpoint": eid, "backend": backend, "model": model,
           "category": "ERROR", "status": None, "markers": [], "detail": "",
           "judge_correct": None, "judge_quality": None, "judge_reason": "",
           "error_code": None, "state": None, "log_file": None}
    # Hoisted so the finally block can persist the full failure context.
    status: dict = {}
    result: dict = {}
    summary = ""
    port = _free_port()
    name = f"bench-{task.label}-{uuid.uuid4().hex[:6]}"
    cenv = {k: _expand(str(v), dotenv) for k, v in (endpoint.get("env", {}) or {}).items()}
    gh_token = _gh_token(dotenv)
    docker_env: list[str] = []
    for k, v in cenv.items():
        docker_env += ["-e", f"{k}={v}"]
    docker_env += ["-e", f"GITHUB_TOKEN={gh_token}",
                   "-e", "PERFORMER_SECRET_SOURCE_INIT_PAYLOAD=1",
                   "-e", "PERFORMER_SECRET_SOURCE_ENV=1",
                   # keep the workspace so post-job graders can inspect committed
                   # plan files / test results / docs (the job loop would
                   # otherwise tear the stand down before we read it).
                   "-e", "PERFORMER_KEEP_STAND=1"]
    extra_secrets: dict[str, str] = {}
    key_name = {"codex": "OPENAI_API_KEY", "claude_code": "ANTHROPIC_AUTH_TOKEN"}.get(
        backend.replace("-", "_"))
    if key_name and (master := (dotenv.get("LITELLM_MASTER_KEY") or "").strip()):
        extra_secrets[key_name] = master
        docker_env += ["-e", f"{key_name}={master}"]
    image = endpoint.get("image", IMAGE)
    cid = None
    try:
        run = subprocess.run(
            ["docker", "run", "-d", "--name", name, "-p", f"{port}:8088", *docker_env, image],
            capture_output=True, text=True, timeout=60)
        if run.returncode != 0:
            res["detail"] = f"docker run failed: {run.stderr.strip()[:120]}"
            return res
        cid = run.stdout.strip()
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + READY_TIMEOUT_S
        ready = False
        while time.time() < deadline:
            with contextlib.suppress(Exception):
                if httpx.get(f"{base}/healthz", timeout=5).status_code == 200:
                    ready = True
                    break
            time.sleep(3)
        if not ready:
            res["detail"] = "container never ready"
            return res
        job_id = uuid.uuid4().hex
        branch = task.branch or f"bench-{task.label}-{uuid.uuid4().hex[:8]}"
        payload = {
            "job_id": job_id, "card_id": f"bench-{task.label}", "role": task.role,
            "backend": backend, "persona": task.persona, "repo_url": repo_url,
            "branch": branch,
            "secrets": {"GITHUB_TOKEN": gh_token, **extra_secrets},
            "metadata": {"title": task.title, "description": task.description,
                         "acceptance_criteria": task.acceptance, "model": model},
        }
        if task.pr_key:
            pr_url = (prs or {}).get(task.pr_key)
            if not pr_url:
                res["detail"] = f"no pr_url for '{task.pr_key}' in --prs"
                res["category"] = "FAIL_HARNESS"
                return res
            payload["metadata"]["pr_url"] = pr_url
            payload["pr_url"] = pr_url
        pr = httpx.post(f"{base}/jobs", json=payload, timeout=15)
        if pr.status_code != 202:
            res["detail"] = f"POST /jobs -> {pr.status_code}: {pr.text[:120]}"
            res["category"] = "FAIL_HARNESS"
            return res
        status = {}
        deadline = time.time() + JOB_TIMEOUT_S
        while time.time() < deadline:
            with contextlib.suppress(Exception):
                jr = httpx.get(f"{base}/jobs/{job_id}", timeout=10)
                if jr.status_code == 200:
                    status = jr.json()
                    if status.get("state") in ("succeeded", "failed", "cancelled"):
                        break
            time.sleep(5)
        result = status.get("result") or {}
        summary = result.get("summary") or ""
        parsed: dict = {}
        with contextlib.suppress(Exception):
            parsed = json.loads(summary) if summary.strip().startswith("{") else {}
        # Locate the cloned bench repo by its unique marker (BENCH.md), which is
        # unambiguous vs. picking the first .git dir (env-cache also has one).
        repo_subdir = _docker_exec(
            cid, "find /tmp /root /home /workspace -maxdepth 6 -name BENCH.md 2>/dev/null "
            "| head -1 | xargs -r dirname") or _docker_exec(
            cid, "find /tmp /root /workspace -maxdepth 4 -name .git -type d 2>/dev/null "
            "| head -1 | xargs -r dirname") or "/workspace"
        ctx = GradeCtx(
            role=task.role, state=status.get("state"),
            status=parsed.get("status") or result.get("error_code") or "",
            summary=summary, parsed=parsed, error_code=result.get("error_code"),
            cid=cid, repo_subdir=repo_subdir)
        contract_ok, markers, detail = task.grader(ctx)
        res.update(status=ctx.status, markers=markers, detail=detail)

        # Categorise: harness vs model vs pass.
        blob = f"{summary} {result.get('error_code','')} {ctx.status}"
        jc = jq = None
        jr_reason = ""
        if judge_cfg and ctx.state == "succeeded":
            jc, jq, jr_reason = judge(task, ctx, detail, **judge_cfg)
            res.update(judge_correct=jc, judge_quality=jq, judge_reason=jr_reason)
        category, harness_hit = categorize(ctx.state, blob, contract_ok, markers, jc)
        res["category"] = category
        res["state"] = ctx.state
        res["error_code"] = result.get("error_code")
        if harness_hit:
            res["detail"] = f"{detail} | harness:{','.join(harness_hit)}"
        return res
    except Exception as exc:
        res["detail"] = f"{type(exc).__name__}: {exc}"[:160]
        return res
    finally:
        if cid:
            # Persist full failure context for every non-PASS cell so we can tell
            # WHAT needs fixing: the job result (agent progress/error), the
            # performer's logs_excerpt, and the container's stderr (where crashes
            # / tracebacks actually surface — not in the job summary).
            if res["category"] != "PASS":
                clogs = subprocess.run(["docker", "logs", "--tail", "400", cid],
                                       capture_output=True, text=True, timeout=20)
                container_logs = ((clogs.stdout or "") + (clogs.stderr or ""))[-8000:]
                celldir = outdir / "cells"
                with contextlib.suppress(Exception):
                    celldir.mkdir(parents=True, exist_ok=True)
                    fname = f"{task.label}__{eid}.json"
                    (celldir / fname).write_text(json.dumps({
                        "row": res,
                        "state": status.get("state"),
                        "error_code": result.get("error_code"),
                        "logs_excerpt": result.get("logs_excerpt"),
                        "summary": summary,
                        "container_logs": container_logs,
                    }, indent=2, default=str))
                    res["log_file"] = f"cells/{fname}"
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="bench repo clone URL")
    ap.add_argument("--prs", help="json file mapping {role: pr_url}")
    ap.add_argument("--config", default=str(REPO_ROOT / "config.yaml"))
    ap.add_argument("--roles", help="comma list of role labels (default: all)")
    ap.add_argument("--backends", help="comma list of endpoint ids (default: all)")
    ap.add_argument("--judge-model", help="LiteLLM model for the judge, e.g. spark/gpt-oss:120b")
    ap.add_argument("--out", default=str(REPO_ROOT / "specs" / "077-multi-backend-qa" / "persona_runs"),
                    help="committed results root (durable, in git)")
    ap.add_argument("--tag", default="run", help="subdir under --out (namespaces a run, e.g. the model)")
    ap.add_argument("--rerun-failures", action="store_true",
                    help="re-run ONLY the non-PASS (role,backend) cells from this tag's existing "
                         "results.json and merge them back (preserving PASS rows) — used to backfill "
                         "failure logs without re-running passing cells")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    dotenv = _load_env(REPO_ROOT / ".env")
    prs = json.loads(Path(args.prs).read_text()) if args.prs else {}
    endpoints = cfg.get("performer_endpoints", []) or []
    if args.backends:
        want = set(args.backends.split(","))
        endpoints = [e for e in endpoints if e["id"] in want]
    tasks = TASKS
    if args.roles:
        want = set(args.roles.split(","))
        tasks = [t for t in tasks if t.label in want]

    judge_cfg = None
    if args.judge_model:
        base = (dotenv.get("COORDINARE_INFERENCE_BASE_URL") or
                "https://litellm.vividynamics.com/v1")
        judge_cfg = {"base_url": base,
                     "api_key": (dotenv.get("LITELLM_MASTER_KEY") or "").strip(),
                     "model": args.judge_model}

    outdir = Path(args.out) / args.tag
    outdir.mkdir(parents=True, exist_ok=True)

    # Build the list of (task, endpoint) cells to run. Default = full grid.
    # --rerun-failures restricts it to the non-PASS cells of the existing run
    # and seeds `merged` with the prior rows so PASS cells are preserved.
    prior: list[dict] = []
    merged: dict[tuple[str, str], dict] = {}
    pairs: list[tuple] = [(t, e) for t in tasks for e in endpoints]
    if args.rerun_failures:
        prior_path = outdir / "results.json"
        if not prior_path.is_file():
            print(f"--rerun-failures: no prior results at {prior_path}")
            return 1
        prior = json.loads(prior_path.read_text())
        merged = {(r["role"], r["endpoint"]): r for r in prior}
        failset = {(r["role"], r["endpoint"]) for r in prior if r["category"] != "PASS"}
        pairs = [(t, e) for (t, e) in pairs if (t.label, e["id"]) in failset]
        print(f"--rerun-failures: {len(pairs)} non-PASS cell(s) to re-run\n")

    print(f"Persona bench [{args.tag}]: {len(pairs)} cell(s), "
          f"judge={'on' if judge_cfg else 'off'}\n")
    for t, e in pairs:
        print(f"  -> {t.label:<13} on {e['id']:<20} ...", flush=True)
        r = run_cell(t, e, cfg, dotenv, args.repo, prs, judge_cfg, outdir)
        merged[(t.label, e["id"])] = r
        jr = f" judge={r['judge_correct']}({r['judge_quality']})" if r["judge_correct"] is not None else ""
        print(f"     {r['category']:<13} status={r['status']} "
              f"markers={r['markers']}{jr} {r['detail'][:70]}", flush=True)
    results = sorted(merged.values(), key=lambda r: (r["role"], r["endpoint"]))
    (outdir / "results.json").write_text(json.dumps(results, indent=2, default=str))

    # Matrix summary (stdout) + a committed, analysis-ready persona x backend grid.
    icon = {"PASS": "✅", "FAIL_MODEL": "❌model", "FAIL_HARNESS": "🔧harness", "ERROR": "💥"}
    short = {"PASS": "✅", "FAIL_MODEL": "❌", "FAIL_HARNESS": "🔧", "ERROR": "💥"}
    backends = sorted({r["endpoint"].replace("-ephemeral", "") for r in results})
    print("\n=== PERSONA x BACKEND ===")
    for t in tasks:
        cells = [r for r in results if r["role"] == t.label]
        line = "  ".join(f"{r['endpoint'].replace('-ephemeral','')}:{icon.get(r['category'],'?')}"
                         for r in cells)
        print(f"{t.label:<13} {line}")
    counts: dict[str, int] = {}
    for r in results:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    print("\nTotals:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    # results.md — pivoted grid + per-persona viability, so reports compose from
    # a glance and the flat results.json supports any cross-cut (model/backend/
    # persona). Each cell shows category + the model that backend ran on.
    by = {(r["role"], r["endpoint"].replace("-ephemeral", "")): r for r in results}
    md = [f"# Persona benchmark — `{args.tag}`", "",
          "_✅ PASS · ❌ FAIL_MODEL (ran, got it wrong) · 🔧 FAIL_HARNESS (fixable "
          "plumbing) · 💥 ERROR. Cell shows category + the model that backend ran._", "",
          "| persona | " + " | ".join(backends) + " |",
          "|---|" + "|".join("---" for _ in backends) + "|"]
    for t in tasks:
        cells = []
        for b in backends:
            r = by.get((t.label, b))
            if not r:
                cells.append("-")
                continue
            mdl = (r.get("model") or "").replace("spark/", "")
            cells.append(f"{short.get(r['category'], '?')} {mdl}")
        md.append(f"| {t.label} | " + " | ".join(cells) + " |")
    md += ["", "## Per-persona — which (backend, model) PASSed", ""]
    for t in tasks:
        winners = [f"{r['endpoint'].replace('-ephemeral','')}({(r.get('model') or '').replace('spark/','')})"
                   for r in results if r["role"] == t.label and r["category"] == "PASS"]
        md.append(f"- **{t.label}**: {', '.join(winners) if winners else '— none passed —'}")
    md += ["", "## Totals", "", ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))]
    (outdir / "results.md").write_text("\n".join(md) + "\n")
    print(f"Full results: {outdir / 'results.json'}  +  {outdir / 'results.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
