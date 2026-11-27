#!/usr/bin/env python3
"""Persona-capability benchmark (077): role x backend x model, graded.

For every lifecycle role (assessor, architect, implementer, reviewer, security,
qa, tech_writer, closer, env_bootstrap) and the card-less advocate this
dispatches a *deliberately difficult* task with a known-correct answer or a planted issue to a backend on a
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

391: every payload is built by the production builders in
``coordinare.services.http_performer_service`` from a card context shaped like
the one the daemon builds (``dispatch_performer`` for card roles, the env-cache
bootstrap for ``env_bootstrap``, ``intake_dispatch`` for the advocate). So the
bench carries the security scan request, the PR diff, the reviewed environment
contract, the nare rail metadata and the role workflow, exactly as production
does. Each cell saves the performer's job transcript (the raw backend output)
as ``cells/<label>__<endpoint>.raw.jsonl`` beside its cell JSON.

Usage:
  scripts/persona_bench.py --repo <git_url> --prs tmp/bench_prs.json \\
      [--config config.yaml] [--roles reviewer,security,qa] \\
      [--backends codex-ephemeral,...] [--judge-model local/gpt-oss:120b]
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
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
    "WorkspaceSetupError", "auth_failed", "empty output",
)

# A JSON-contract parse failure (BACKEND_FORMAT_ERROR) is NOT, by itself, a
# harness signature: the coordinare emits it only after the transport delivered
# the model's bytes and the model refused to shape them into the contract. The
# performer appends `Last output: <repr>` — a NON-EMPTY repr means the model
# spoke (model-quality miss → FAIL_MODEL); an EMPTY repr ('' / "") means nothing
# came back (swallowed / transport wall → FAIL_HARNESS).
_FORMAT_ERR_EMPTY_RE = re.compile(r"Last output:\s*(?:''|\"\")")


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
    # 391: what the bench set up for this cell (e.g. the advocate's issue numbers).
    fixture: dict = field(default_factory=dict)


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
    # BACKEND_FORMAT_ERROR disambiguation (precedes the generic harness rule):
    # the model emitting non-empty-but-non-contract output is a model-quality
    # miss, not plumbing. Only an empty last output is a transport/swallowed wall.
    if "BACKEND_FORMAT_ERROR" in blob:
        if _FORMAT_ERR_EMPTY_RE.search(blob):
            return "FAIL_HARNESS", [*harness_hit, "BACKEND_FORMAT_ERROR(empty)"]
        return "FAIL_MODEL", harness_hit
    if harness_hit and (failed or not contract_ok):
        return "FAIL_HARNESS", harness_hit
    if failed and not contract_ok:
        return "ERROR", harness_hit
    correct = judge_correct if judge_correct is not None else (contract_ok and bool(markers))
    return ("PASS" if (contract_ok and correct) else "FAIL_MODEL"), harness_hit


def reconciliation_note(state: str | None, error_code: str | None,
                        category: str) -> str | None:
    """FR-009 reconciliation witness.

    categorize() can legitimately route a job that reported a terminal failure
    (state in failed/cancelled) or carried error_code=="error" up to PASS when
    the contract artifact genuinely holds (e.g. a real in-workspace pytest
    "4 passed" satisfies the implementer contract even though a post-success
    CI/PR-handoff step flipped the job to error). That override is correct but
    MUST NOT be silent — FR-009 requires it be explicit and documented.

    Returns a human-readable note when a PASS verdict masks a failed/cancelled
    state or an error_code; None otherwise (clean PASS, or any non-PASS cell
    whose failure is already self-evident in its own category).
    """
    if category != "PASS":
        return None
    flags = []
    if state in ("failed", "cancelled"):
        flags.append(f"state={state}")
    if error_code == "error":
        flags.append("error_code=error")
    if not flags:
        return None
    return ("reconciled-to-PASS: contract artifact satisfied despite "
            + ", ".join(flags) + " (see container_logs for the post-success failure)")


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


def grade_documenter(c: GradeCtx) -> tuple[bool, list[str], str]:
    # 124: the documenter maintains a living docs/wiki record of truth. Contract
    # = docs_committed AND a README entrypoint plus at least one section page
    # produced under docs/wiki/.
    wiki = _docker_exec(
        c.cid,
        f"cd {c.repo_subdir} 2>/dev/null && "
        f"(test -f docs/wiki/README.md && find docs/wiki -name '*.md' 2>/dev/null | head -3)",
    )
    contract_ok = c.status == "docs_committed" and bool(wiki)
    return contract_ok, (["wiki_pages_present"] if wiki else []), f"status={c.status} wiki={bool(wiki)}"


def grade_closer(c: GradeCtx) -> tuple[bool, list[str], str]:
    # Clean, correct PR → closer should reach a terminal review verdict.
    contract_ok = c.status in ("approved", "changes_requested", "qa_passed")
    return contract_ok, ([c.status] if contract_ok else []), f"status={c.status}"


def grade_env_bootstrap(c: GradeCtx) -> tuple[bool, list[str], str]:
    # 391: the bootstrap installs the independently reviewed manifest, and the
    # performer then runs that manifest's own checks. The contract is the
    # complete status AND those checks passing, never the agent's prose.
    report = c.parsed.get("report") if isinstance(c.parsed.get("report"), dict) else {}
    verification = report.get("environment_verification") or {}
    passed = isinstance(verification, dict) and verification.get("passed") is True
    contract_ok = c.status == "env_bootstrap_complete" and passed
    mk = ["manifest_checks_passed"] if passed else []
    results = verification.get("results") if isinstance(verification, dict) else None
    ran = len(results) if isinstance(results, list) else 0
    return contract_ok, mk, f"status={c.status} checks_passed={passed} results={ran}"


def grade_advocate(c: GradeCtx) -> tuple[bool, list[str], str]:
    # 391: two fixture issues per run. The README answers one (how to run the
    # tests); nothing documents the other, so it must reach a human.
    report = c.parsed.get("report") if isinstance(c.parsed.get("report"), dict) else {}
    record = report.get("advocate") if isinstance(report.get("advocate"), dict) else {}
    outcomes = {int(o.get("number") or 0): o for o in record.get("outcomes") or [] if isinstance(o, dict)}
    answerable = outcomes.get(int(c.fixture.get("answerable") or -1)) or {}
    unanswerable = outcomes.get(int(c.fixture.get("unanswerable") or -1)) or {}
    # Markers are evidence of the correct pair only, since without a judge PASS
    # rests on contract_ok and markers; the contract is that the run reported
    # an outcome for both issues, so a judge still decides an unexpected one.
    correct = answerable.get("action") == "replied" and unanswerable.get("action") == "escalated"
    mk = ["answered_documented_question", "escalated_undocumented_question"] if correct else []
    contract_ok = c.status == "advocate_complete" and bool(answerable) and bool(unanswerable)
    flag = " ANSWERED_WITHOUT_DOCS" if unanswerable.get("action") == "replied" else ""
    return contract_ok, mk, (
        f"status={c.status} answerable={answerable.get('action')} "
        f"unanswerable={unanswerable.get('action')}{flag}"
    )


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
    # 391: the performer config key (persona, mode, workflow). Defaults to the
    # label; two tasks for one role need distinct labels so results.json keeps
    # both, since results are keyed by (label, endpoint).
    performer: str = ""
    # 391: how the cell is dispatched: "card" (the lifecycle dispatch),
    # "bootstrap" (the env-cache bootstrap) or "intake" (the card-less advocate).
    dispatch: str = "card"

    @property
    def config_key(self) -> str:
        return self.performer or self.label


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
        "security", "security", "bench/security-sqli-vuln", "security",
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
        "tech_writer_wiki", "documenting", "feat/techwriter-wiki", None,
        "Maintain the living project wiki",
        "Recent changes added new modules with no docs/wiki/ coverage. "
        "Build/update the docs/wiki/ record of truth (README.md entrypoint + "
        "section pages, and AGENTS.md/CLAUDE.md pointers) so it covers the "
        "repository for other agents to read.",
        ["docs/wiki/ documents the repository"],
        "You are the documenter. Maintain docs/wiki/ as the project's living "
        "record of truth for other agents to read.",
        grade_documenter,
        "CORRECT = docs/wiki/ has a README entrypoint plus accurate section pages "
        "describing the repo (modules, workflows). WRONG = no docs/wiki/ produced "
        "or empty/irrelevant.",
        performer="tech_writer",
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
        "CORRECT = the reviewed manifest's checks pass after the install (pytest "
        "runnable from the cache). WRONG = bootstrap reports done but its checks "
        "fail, or it rewrote the checks instead of installing.",
        dispatch="bootstrap",
    ),
    RoleTask(
        "advocate", "advocate", "", None,
        "Answer inbound issues",
        "Classify the repository's open inbound issues and answer the ones the "
        "documentation covers, escalating everything else to a human.",
        ["Documented questions answered from the docs; the rest escalated"],
        "You are the advocate.",
        grade_advocate,
        "Two open issues. One asks how to run the test suite, which README.md "
        "answers (pip install -e .[test], then pytest -q). The other asks about a "
        "Terraform module for AWS Lambda that no document mentions. CORRECT = "
        "reply to the first citing README.md, and escalate the second to a human. "
        "WRONG = answer the second from nothing, or escalate the first.",
        dispatch="intake",
    ),
]


# ---------------------------------------------------------------------------
# LLM judge (over the LiteLLM proxy).
# ---------------------------------------------------------------------------
def _judge_eligible(ctx: GradeCtx) -> bool:
    """Whether a cell has gradeable agent output for the LLM judge.

    A job that reached terminal 'succeeded' always qualifies. A gate-refused
    cell ends ``state='failed'`` yet still carries a full structured terminal
    report (``ctx.parsed`` — e.g. the qwen3-14b rubber-stamp that the evidence
    guard flipped to ``qa_failed``); that must be judged too, so the refusal is
    behaviourally confirmed instead of falling through to the deterministic-only
    category. A genuinely crashed/cancelled job (no structured report) has
    nothing to grade and is skipped."""
    if ctx.state == "succeeded":
        return True
    return bool(ctx.parsed)


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
# 080-aware dispatch resolution + payload assembly.
#
# The live coordinare resolves a role's dispatch model from its mode (modes →
# model_endpoints → endpoints) and, for non-`single` strategies, carries a full
# `orchestration` block so the in-container DualModelProxy launches the
# planner/executor pair (dispatch_performer.py). Pre-080 this harness read the
# legacy inline `performers.<role>.model` and never injected `orchestration`, so
# dual-model modes silently degraded to single-model. These helpers mirror the
# live wiring so the bench actually validates the planner+executor combo.
# ---------------------------------------------------------------------------
def resolve_role_dispatch(config_path: str, role: str,
                          cfg: dict | None = None, *, backend: str = "",
                          dotenv: dict | None = None) -> dict:
    """Resolve a role's dispatch model, its full model block, orchestration and
    workflow from the coordinare config catalogs, mirroring ``dispatch_performer``.
    ``role`` is the performer key (e.g. ``architect``). Falls back to the pre-080
    inline ``performers.<role>.model`` when the config has no mode for the role.

    391: ``model_block`` is everything ``resolve_performer_dispatch_model`` returns
    (``endpoint_kind``, ``base_url``, ``auth_token_env``, ``api_key_env``,
    ``reasoning_policy``), which production spreads into the card context and the
    nare adapter reads to pick its rail. ``workflow_env`` and ``tuning`` (effort,
    thinking, temperature) are what ``_apply_role_tuning`` adds; the tuning is
    translated for ``backend``, the one the cell runs, since production always
    translates for the backend it dispatches. ``effort`` and ``temperature`` are
    the raw role values the daemon's bootstrap dispatch sends. The config loads
    under ``.env``, as the daemon's does, so ``${VAR}`` placeholders expand."""
    model = ""
    orchestration = None
    model_block: dict = {}
    workflow = ""
    workflow_env: dict = {}
    tuning: dict = {}
    effort = temperature = None
    try:
        from coordinare.config import ProjectConfiguration
        from coordinare.env_placeholders import operator_workflow_env
        from coordinare.services.performer_tuning import translate_tuning
        with _daemon_env(dotenv or {}):
            pc = ProjectConfiguration.from_yaml(config_path)
            model_block = dict(pc.resolve_performer_dispatch_model(role) or {})
            model = model_block.get("model", "") or ""
            orchestration = pc.resolve_performer_orchestration(role)
            rc = pc.performers.resolved_role(role)
            if rc is not None:
                workflow = str(getattr(rc, "workflow", "") or "")
                if getattr(rc, "workflow_env", None):
                    workflow_env = operator_workflow_env(rc)
                effort, temperature = rc.effort, rc.temperature
                cell_rc = rc.model_copy(update={"backend": backend}) if backend else rc
                tuning = dict(translate_tuning(cell_rc))
    except Exception:
        pass
    if not model and cfg is not None:
        rc = (cfg.get("performers", {}) or {}).get(role)
        if isinstance(rc, dict) and rc.get("model"):
            model = str(rc["model"])
    return {"model": model, "orchestration": orchestration, "model_block": model_block,
            "workflow": workflow, "workflow_env": workflow_env, "tuning": tuning,
            "effort": effort, "temperature": temperature}


def _prod_persona(role: str, config_path: str | None) -> str | None:
    """Resolve the REAL production persona for ``role`` (a performer key) from
    config (the effective per-role instructions, e.g. spec-083's CWE taint→sink
    checklist for ``security``). Returns None if it cannot be resolved, so callers
    fall back to the task's inline persona. This is what makes the bench actually
    exercise the shipped persona instead of a generic one-liner."""
    if not config_path:
        return None
    try:
        from coordinare.config import ProjectConfiguration
        from coordinare.services.persona_service import (
            VALID_ROLES,
            get_effective_instructions,
        )
        if role not in VALID_ROLES:
            return None
        pc = ProjectConfiguration.from_yaml(config_path)
        return get_effective_instructions(role, pc.personas)
    except Exception:
        return None


#: 391: the transcript byte cap the bench asks for, so each cell's raw backend
#: output survives the run (the performer records nothing without a cap).
TRANSCRIPT_MAX_BYTES = 8 * 1024 * 1024


def build_card_context(task: RoleTask, *, backend: str, repo_url: str, branch: str,
                       model: str, model_block: dict | None = None,
                       orchestration: dict | None = None, workflow: str = "",
                       workflow_env: dict | None = None, tuning: dict | None = None,
                       pr_url: str | None = None, persona: str | None = None,
                       extra: dict | None = None) -> dict:
    """The card context a lifecycle dispatch hands the transport (391).

    The keys ``dispatch_performer`` sets for a github.com card: ``id``, ``role``,
    ``backend``, ``persona_instructions``, the card text, ``workflow`` and
    ``workflow_env`` when the role runs one, the role's model block spread in,
    ``orchestration`` for non-single strategies, the role tuning, and ``pr_url``
    for PR roles. ``extra`` carries the PR context (``pr_card_context``). Not
    set: the GitHub Enterprise URLs, board state, disputes and other card
    history a first dispatch has none of."""
    context: dict = {
        "id": f"bench-{task.label}", "role": task.role, "backend": backend,
        "persona_instructions": persona or task.persona,
        "repo_url": repo_url, "branch": branch,
        "title": task.title, "description": task.description,
        "acceptance_criteria": task.acceptance,
    }
    if workflow:
        context["workflow"] = workflow
    if workflow_env:
        context["workflow_env"] = dict(workflow_env)
    context.update(model_block or {})
    if model:
        context["model"] = model
    if orchestration is not None:
        context["orchestration"] = orchestration
    context.update(tuning or {})
    if pr_url:
        context["pr_url"] = pr_url
    context.update(extra or {})
    return context


@contextlib.contextmanager
def _daemon_env(dotenv: dict):
    """The daemon's process env for one payload build: ``.env`` under the shell.

    The production builders read provider keys from ``os.environ`` (the daemon
    loads ``.env`` without overriding its own env), so the bench does the same
    for the duration of the build and restores everything afterwards."""
    added = [k for k in dotenv if k not in os.environ]
    for key in added:
        os.environ[key] = str(dotenv[key])
    try:
        yield
    finally:
        for key in added:
            os.environ.pop(key, None)


def build_job_payload(card_context: dict, *, gh_token: str, dotenv: dict | None = None,
                      transcript_max_bytes: int | None = TRANSCRIPT_MAX_BYTES) -> dict:
    """The ``/jobs`` body, built by the production builder (391).

    A card dispatch goes through ``build_job_payload`` with the GitHub token in
    the workspace info, as ``WorkspaceManager.prepare`` supplies it; a context
    with ``job_type: env_bootstrap`` routes to the bootstrap builder there, as it
    does in production. Secrets are revealed for the wire."""
    from coordinare.services.http_performer_service import build_job_payload as _build
    from coordinare.workspace import WorkspaceInfo

    info = None
    if card_context.get("job_type") != "env_bootstrap":
        info = WorkspaceInfo(path=None, branch=str(card_context.get("branch") or ""),
                             repo_url=str(card_context.get("repo_url") or ""),
                             github_token=gh_token)
    with _daemon_env(dotenv or {}):
        payload = _build(card_context, info)
        master = (os.environ.get("LITELLM_MASTER_KEY") or "").strip()
    wire = payload.model_dump(mode="json", exclude={"secrets"})
    wire["secrets"] = {k: v.get_secret_value() for k, v in payload.secrets.items()}
    _legacy_gateway_key(wire, card_context, master)
    if transcript_max_bytes is not None:
        wire["transcript_max_bytes"] = transcript_max_bytes
    return wire


def _legacy_gateway_key(wire: dict, card_context: dict, master: str) -> None:
    """Keep the pre-391 bench's gateway key for an endpoint with no routed mode.

    A pre-080 bench config points codex or claude_code at the gateway through
    the endpoint's container env and has no mode for the builder to route by,
    so the builder would send the vendor keys from ``.env``, which the
    performer prefers over the container env. As the bench always did, the
    gateway's master key goes in their place. A routed mode is left alone."""
    backend = str(card_context.get("backend") or "").replace("-", "_")
    routed = any(card_context.get(k) for k in ("base_url", "auth_token_env", "api_key_env", "endpoint_kind"))
    if routed or not master or backend not in ("codex", "claude_code"):
        return
    secrets = wire["secrets"]
    if backend == "codex":
        secrets["OPENAI_API_KEY"] = master
    else:
        secrets.pop("ANTHROPIC_API_KEY", None)
        secrets["ANTHROPIC_AUTH_TOKEN"] = master
    wire.setdefault("metadata", {})["environment_secret_names"] = sorted(secrets)


# ---------------------------------------------------------------------------
# 391: PR context, the way dispatch_performer injects it.
# ---------------------------------------------------------------------------
def fetch_pr_diff(pr_url: str, token: str) -> tuple[str, list[str], str]:
    """``(raw_diff, changed_files, head_sha)`` for a PR, via the REST API.

    Changed files come from the diff headers with ``GitHubService``'s own parser,
    as ``get_pr_diff`` does."""
    from coordinare.services.github import GitHubService

    m = re.match(r"https?://[^/]+/([^/]+)/([^/]+)/pull/(\d+)", pr_url or "")
    if not m:
        raise ValueError(f"unrecognized PR URL: {pr_url!r}")
    owner, repo, number = m.group(1), m.group(2), m.group(3)
    headers = {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"
    diff = httpx.get(url, headers={**headers, "Accept": "application/vnd.github.diff"}, timeout=30)
    diff.raise_for_status()
    meta = httpx.get(url, headers={**headers, "Accept": "application/vnd.github+json"}, timeout=30)
    meta.raise_for_status()
    raw = diff.text or ""
    return raw, GitHubService._parse_diff_paths(raw), str(meta.json()["head"]["sha"])


def pr_card_context(performer_key: str, raw_diff: str | None, files: list[str] | None,
                    head_sha: str | None, *, workflow: str = "", status: str = "") -> dict:
    """The PR keys ``dispatch_performer._inject_pr_context`` adds (391).

    The diff roles and security get the sanitized diff, its status and the
    changed paths. Prose security (no security workflow) also gets the
    mandatory scan request (#380), bound to the verified head; a head that is
    unknown leaves it unbound, which the performer refuses, as in production."""
    from coordinare.graph.nodes.dispatch_performer import (
        _DIFF_REVIEW_ROLES,
        _MAX_DISPATCH_CHANGED_PATHS,
        _sanitize_pr_diff,
        _security_floor_request,
    )

    if performer_key not in _DIFF_REVIEW_ROLES and performer_key != "security":
        return {}
    if raw_diff is None:
        pr_data: tuple[str | None, list[str] | None, str] = (None, None, status or "failed")
    else:
        sanitized = _sanitize_pr_diff(raw_diff) if raw_diff.strip() else ""
        pr_data = (sanitized or None, list(files or []), "injected" if sanitized.strip() else "empty")
    out: dict = {"pr_diff_status": pr_data[2]}
    if performer_key == "security" and workflow != "security":
        out["security_floor"] = _security_floor_request(pr_data, head_sha)
    if pr_data[1]:
        out["pr_changed_paths"] = pr_data[1][:_MAX_DISPATCH_CHANGED_PATHS]
        if len(pr_data[1]) > _MAX_DISPATCH_CHANGED_PATHS:
            out["pr_changed_paths_overflow"] = True
    if pr_data[0]:
        out["pr_diff"] = pr_data[0]
    return out


# ---------------------------------------------------------------------------
# 391: env_bootstrap, the way the env-cache service dispatches it. Discovery and
# independent review run against the fixture repository at one commit, then
# the accepted contract and its rendered scripts ride the bootstrap payload.
# ---------------------------------------------------------------------------
BOOTSTRAP_SYMPHONY = "conductor-bench"
BOOTSTRAP_CACHE_PATH = f"/devenv/{BOOTSTRAP_SYMPHONY}"


def _git_auth_env(repo_url: str, token: str) -> dict[str, str]:
    """The process env with the token as a git http header, never in argv."""
    import base64

    env = dict(os.environ)
    if token and repo_url.startswith("https://"):
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"})
    return env


def clone_snapshot(repo_url: str, token: str, dest: Path) -> tuple[str, list[str]]:
    """Shallow-clone the default branch; return ``(commit, tracked paths)``.

    The token rides the env, and a failure is reported without the command, so
    nothing secret reaches the results."""
    try:
        subprocess.run(["git", "clone", "-q", "--depth", "1", repo_url, str(dest)],
                       check=True, capture_output=True, timeout=120, env=_git_auth_env(repo_url, token))
        commit = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], check=True,
                                capture_output=True, text=True).stdout.strip()
        listing = subprocess.run(["git", "-C", str(dest), "ls-tree", "-r", "-z", "--name-only", "HEAD"],
                                 check=True, capture_output=True, text=True).stdout
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        if token:
            stderr = stderr.replace(token, "***")
        detail = " ".join(stderr.split())[-160:]
        raise RuntimeError(f"git failed to read the fixture snapshot (exit {exc.returncode}): {detail}") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("git timed out reading the fixture snapshot") from None
    return commit, [p for p in listing.split("\0") if p]


def discovery_chat(config_path: str | None, dotenv: dict, judge_cfg: dict | None):
    """The JSON-chat seam production uses for discovery and review.

    The daemon builds it from ``conducting`` when that backend is
    ``openai_api``; without one the bench falls back to the judge's gateway and
    model. None when neither is available (the bootstrap cell then cannot run)."""
    from coordinare.services.conducting import OpenAiApiBackend

    cc = None
    if config_path:
        try:
            with _daemon_env(dotenv or {}):
                from coordinare.config import ProjectConfiguration
                cc = ProjectConfiguration.from_yaml(config_path).conducting
        except Exception as exc:
            print(f"     (could not load conducting from {config_path}: {type(exc).__name__}: {exc})"[:300],
                  flush=True)
    if cc is not None and getattr(cc, "backend", None) == "openai_api":
        key_name = cc.api_key_env or "OPENAI_API_KEY"
        return OpenAiApiBackend(
            api_key=os.environ.get(key_name) or dotenv.get(key_name), model=cc.model or "gpt-4o-mini",
            max_tokens=cc.max_tokens, temperature=cc.temperature, base_url=cc.base_url, effort=cc.effort,
        ).chat_json
    if judge_cfg:
        return OpenAiApiBackend(api_key=judge_cfg["api_key"], model=judge_cfg["model"],
                                base_url=judge_cfg["base_url"]).chat_json
    return None


def bootstrap_card_context(*, repo_dir: Path, commit: str, paths: list[str], repo_url: str,
                           chat, backend: str, gh_token: str, model_block: dict | None = None,
                           orchestration: dict | None = None, workflow: str = "",
                           cache_mount_path: str = BOOTSTRAP_CACHE_PATH,
                           symphony_name: str = BOOTSTRAP_SYMPHONY) -> dict:
    """The bootstrap dispatch dict the daemon hands the transport (391).

    ``discover_and_review`` derives the manifest and its independent review
    from the snapshot exactly as ``EnvCacheService._build_manifest_artifacts``
    does; the payload is ``BootstrapJobPayload.model_dump()`` plus the role's
    backend, model block, orchestration, workflow and GitHub token, as
    ``_dispatch_bootstrap`` adds them. Raises when discovery does not accept."""
    from coordinare_service_inference.environment.render import render_activate, render_verify

    from coordinare.models.env_cache import BootstrapJobPayload
    from coordinare.services.environment_discovery import discover_and_review

    owner_repo = _parse_owner_repo(repo_url)
    if not owner_repo:
        raise ValueError(f"not a GitHub repository URL: {repo_url!r}")
    root = repo_dir.resolve()

    async def fetch(path: str) -> str | None:
        target = (root / path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return None
        try:
            return target.read_text()
        except (OSError, UnicodeDecodeError):
            return None

    manifest, assessment = asyncio.run(discover_and_review(
        symphony_name=symphony_name, commit=commit, paths=paths, fetch=fetch, chat=chat,
    ))
    readme = asyncio.run(fetch("README.md"))
    payload = BootstrapJobPayload(
        symphony_name=symphony_name, symphony_org=owner_repo[0], symphony_repo=owner_repo[1],
        env_spec_files=["README.md"], env_spec_contents={"README.md": readme} if readme else {},
        cache_mount_path=cache_mount_path,
        environment_manifest=manifest.model_dump(mode="json"),
        environment_assessment=assessment.model_dump(mode="json"),
        environment_manifest_digest=assessment.manifest_digest,
        dependency_checklist="ACCEPTED ENVIRONMENT CONTRACT\n" + manifest.model_dump_json(indent=2),
        verify_provided=True, activate_provided=True,
        rendered_verify_sh=render_verify(manifest, cache_mount_path=cache_mount_path),
        rendered_activate_sh=render_activate(manifest, cache_mount_path=cache_mount_path),
    )
    context = payload.model_dump()
    context.update({k: v for k, v in (model_block or {}).items() if v})
    context["backend"] = backend
    if orchestration:
        context["orchestration"] = orchestration
    if workflow:
        context["workflow"] = workflow
    if gh_token:
        context["_github_token"] = gh_token
    return context


# ---------------------------------------------------------------------------
# 391: the advocate, dispatched card-less the way intake_dispatch builds it.
# Each run opens its own two fixture issues and closes them afterwards, so the
# advocate's handled label never hides an issue from a later run.
# ---------------------------------------------------------------------------
ADVOCATE_ISSUES = {
    "answerable": (
        "[bench] How do I run the tests?",
        "I cloned conductor-bench and want to run its test suite locally. "
        "What do I install and which command runs the tests?",
    ),
    "unanswerable": (
        "[bench] Terraform module for AWS Lambda?",
        "Is there an official Terraform module for deploying conductor-bench to "
        "AWS Lambda, and which variables does it take?",
    ),
}


def advocate_card_context(*, repo_url: str, backend: str, persona: str | None,
                          model_block: dict | None = None, config_path: str | None = None,
                          dotenv: dict | None = None) -> dict:
    """``intake_dispatch.build_card_context`` for the advocate (391).

    The workflow env comes from the config's ``advocate`` block, as the daemon
    builds it. The bench needs the fixture's README read, so a config with no
    documentation sources reads README.md, the block's own default."""
    from coordinare.config import AdvocateConfig
    from coordinare.services.intake_dispatch import build_card_context as _intake_context
    from coordinare.services.intake_dispatch import build_workflow_env

    owner_repo = _parse_owner_repo(repo_url)
    if not owner_repo:
        raise ValueError(f"not a GitHub repository URL: {repo_url!r}")
    role_cfg = AdvocateConfig()
    if config_path:
        try:
            with _daemon_env(dotenv or {}):
                from coordinare.config import ProjectConfiguration
                role_cfg = ProjectConfiguration.from_yaml(config_path).advocate
        except Exception as exc:
            print(f"     (could not load advocate from {config_path}: {type(exc).__name__}: {exc};"
                  " using the defaults)"[:300], flush=True)
    workflow_env = build_workflow_env("advocate", role_cfg)
    if json.loads(workflow_env.get("ADVOCATE_DOC_SOURCES") or "[]") == []:
        workflow_env["ADVOCATE_DOC_SOURCES"] = json.dumps(["README.md"])
    return _intake_context(
        "advocate", symphony_name=BOOTSTRAP_SYMPHONY, org=owner_repo[0], repo=owner_repo[1],
        persona=persona or "", backend=backend, model_block=model_block, workflow_env=workflow_env,
    )


def open_advocate_issues(repo_url: str, token: str) -> dict[str, int]:
    """Open the two fixture issues; returns ``{kind: number}``. Raises on failure."""
    parsed = _parse_owner_repo(repo_url)
    if not parsed:
        raise ValueError(f"not a GitHub repository URL: {repo_url!r}")
    owner, repo = parsed
    numbers: dict[str, int] = {}
    try:
        for kind, (title, body) in ADVOCATE_ISSUES.items():
            r = _gh_api("POST", f"/repos/{owner}/{repo}/issues", token, json_body={"title": title, "body": body})
            if r.status_code != 201:
                raise RuntimeError(f"could not open the {kind} fixture issue (HTTP {r.status_code})")
            numbers[kind] = int(r.json()["number"])
    except Exception:
        close_advocate_issues(repo_url, token, numbers)
        raise
    return numbers


def close_advocate_issues(repo_url: str, token: str, numbers: dict[str, int]) -> None:
    """Best-effort close of this run's fixture issues."""
    parsed = _parse_owner_repo(repo_url)
    if not parsed:
        return
    owner, repo = parsed
    for number in numbers.values():
        with contextlib.suppress(Exception):
            _gh_api("PATCH", f"/repos/{owner}/{repo}/issues/{number}", token,
                    json_body={"state": "closed", "state_reason": "not_planned"})


# ---------------------------------------------------------------------------
# 391: the raw backend output. The performer keeps a full transcript of the job
# when the payload carries a byte cap; the bench reads it back in batches.
# ---------------------------------------------------------------------------
def fetch_transcript(base: str, job_id: str, *, limit: int = 500, max_batches: int = 2000) -> list[dict]:
    """Every transcript record the performer holds for ``job_id``, in order."""
    records: list[dict] = []
    cursor = 0
    for _ in range(max_batches):
        try:
            r = httpx.get(f"{base}/jobs/{job_id}",
                          params={"transcript_after": cursor, "transcript_limit": limit}, timeout=30)
        except Exception:
            break
        if r.status_code != 200:
            break
        batch = (r.json() or {}).get("transcript") or []
        if not batch:
            break
        records.extend(batch)
        last = batch[-1].get("seq")
        if not isinstance(last, int) or last <= cursor:
            break
        cursor = last
    return records


def write_cell_files(outdir: Path, label: str, endpoint_id: str, cell: dict,
                     transcript: list[dict]) -> tuple[str, str]:
    """Write ``cells/<label>__<endpoint>.json`` and its ``.raw.jsonl`` beside it.

    Returns the two paths relative to ``outdir``."""
    celldir = outdir / "cells"
    celldir.mkdir(parents=True, exist_ok=True)
    stem = f"{label}__{endpoint_id}"
    raw_rel = f"cells/{stem}.raw.jsonl"
    (outdir / raw_rel).write_text("".join(json.dumps(r, default=str) + "\n" for r in transcript))
    cell_rel = f"cells/{stem}.json"
    (outdir / cell_rel).write_text(json.dumps({**cell, "raw_file": raw_rel}, indent=2, default=str))
    return cell_rel, raw_rel


# ---------------------------------------------------------------------------
# Read-only fixture isolation. A performer commits + pushes a work/QA report
# to whatever branch it is dispatched onto (it holds GITHUB_TOKEN). For tasks
# pinned to a shared fixture branch (e.g. feat/qa-behavior) that would mutate
# the fixture and corrupt the signal across runs. So we fork a per-run
# throwaway branch from the fixture HEAD, dispatch the performer there, and
# delete it afterwards — the fixture branch stays pristine.
# ---------------------------------------------------------------------------
def _parse_owner_repo(repo_url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a GitHub https/ssh/token-embedded URL."""
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", repo_url.strip())
    return (m.group(1), m.group(2)) if m else None


def _gh_api(method: str, path: str, token: str, *, json_body: dict | None = None):
    """Issue a GitHub REST API call and return the httpx response."""
    return httpx.request(
        method,
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=json_body,
        timeout=20,
    )


def _fork_throwaway_branch(repo_url: str, base_branch: str, new_branch: str,
                           token: str) -> str | None:
    """Create ``new_branch`` on the remote at ``base_branch``'s HEAD sha.

    Returns the forked sha only when the throwaway ref was created (391: the
    security scan request binds to it). Callers must treat None as "do not
    dispatch" so a contaminated fixture is never risked."""
    parsed = _parse_owner_repo(repo_url)
    if not parsed:
        return None
    owner, repo = parsed
    try:
        r = _gh_api("GET", f"/repos/{owner}/{repo}/git/ref/heads/{base_branch}", token)
        if r.status_code != 200:
            return None
        sha = str(r.json()["object"]["sha"])
        c = _gh_api("POST", f"/repos/{owner}/{repo}/git/refs", token,
                    json_body={"ref": f"refs/heads/{new_branch}", "sha": sha})
        return sha if c.status_code in (200, 201) else None
    except Exception:
        return None


def _delete_remote_branch(repo_url: str, branch: str, token: str) -> None:
    """Best-effort delete of a remote ref (used to tear down a throwaway)."""
    parsed = _parse_owner_repo(repo_url)
    if not parsed:
        return
    owner, repo = parsed
    with contextlib.suppress(Exception):
        _gh_api("DELETE", f"/repos/{owner}/{repo}/git/refs/heads/{branch}", token)


class BenchSetupError(RuntimeError):
    """A cell could not be prepared; the cell is FAIL_HARNESS, nothing dispatched."""


@dataclass
class PreparedCell:
    card_context: dict
    fixture: dict = field(default_factory=dict)
    cleanups: list[Callable[[], None]] = field(default_factory=list)


def prepare_cell(task: RoleTask, *, backend: str, repo_url: str, gh_token: str,
                 dispatch: dict, persona: str | None, prs: dict, dotenv: dict,
                 judge_cfg: dict | None, config_path: str | None) -> PreparedCell:
    """Everything a cell needs before its ``/jobs`` POST (391).

    Raises ``BenchSetupError`` when a fixture is missing, so the cell is graded
    as plumbing and nothing is dispatched onto a shared fixture."""
    model = dispatch.get("model", "")
    model_block = dict(dispatch.get("model_block") or {})
    orchestration = dispatch.get("orchestration")
    workflow = str(dispatch.get("workflow") or "")
    workflow_env = dict(dispatch.get("workflow_env") or {})
    tuning = dict(dispatch.get("tuning") or {})
    if task.dispatch == "bootstrap":
        chat = discovery_chat(config_path, dotenv, judge_cfg)
        if chat is None:
            raise BenchSetupError(
                "env_bootstrap needs a discovery model: set conducting.backend: openai_api "
                "in the config, or pass --judge-model")
        with tempfile.TemporaryDirectory(prefix="bench-bootstrap-") as tmp:
            try:
                commit, paths = clone_snapshot(repo_url, gh_token, Path(tmp) / "repo")
                context = bootstrap_card_context(
                    repo_dir=Path(tmp) / "repo", commit=commit, paths=paths, repo_url=repo_url,
                    chat=chat, backend=backend, gh_token=gh_token, model_block={**model_block, "model": model},
                    orchestration=orchestration, workflow=workflow)
            except Exception as exc:
                raise BenchSetupError(f"environment discovery did not accept: {exc}"[:240]) from exc
        # The daemon's bootstrap dispatch adds the role's raw effort and
        # temperature, and its workflow env when the role runs a workflow.
        context.update({k: dispatch[k] for k in ("effort", "temperature") if dispatch.get(k) is not None})
        if workflow:
            context["workflow_env"] = workflow_env
        return PreparedCell(context)
    if task.dispatch == "intake":
        try:
            context = advocate_card_context(repo_url=repo_url, backend=backend, persona=persona,
                                            model_block={**model_block, "model": model},
                                            config_path=config_path, dotenv=dotenv)
        except Exception as exc:
            raise BenchSetupError(f"advocate dispatch could not be built: {exc}"[:240]) from exc
        if orchestration is not None:
            context["orchestration"] = orchestration
        try:
            numbers = open_advocate_issues(repo_url, gh_token)
        except Exception as exc:
            raise BenchSetupError(f"could not open the advocate fixture issues: {exc}"[:240]) from exc
        return PreparedCell(context, fixture=dict(numbers),
                            cleanups=[lambda: close_advocate_issues(repo_url, gh_token, numbers)])

    cleanups: list[Callable[[], None]] = []
    head_sha = None
    if task.branch:
        # Fixture-pinned task: never dispatch onto the shared fixture branch
        # (the performer would push its report back and corrupt it). Fork a
        # per-run throwaway branch from the fixture HEAD and dispatch there.
        branch = f"bench-ro-{task.label}-{uuid.uuid4().hex[:8]}"
        head_sha = _fork_throwaway_branch(repo_url, task.branch, branch, gh_token)
        if not head_sha:
            raise BenchSetupError(f"could not fork throwaway branch from '{task.branch}' "
                                  "(refusing to dispatch onto the shared fixture)")
        cleanups.append(lambda: _delete_remote_branch(repo_url, branch, gh_token))
    else:
        branch = f"bench-{task.label}-{uuid.uuid4().hex[:8]}"
    pr_url = None
    extra: dict = {}
    try:
        if task.pr_key:
            pr_url = (prs or {}).get(task.pr_key)
            if not pr_url:
                raise BenchSetupError(f"no pr_url for '{task.pr_key}' in --prs")
            try:
                raw_diff, files, pr_head = fetch_pr_diff(pr_url, gh_token)
            except Exception as exc:
                raw_diff, files, pr_head = None, None, None
                print(f"     (PR diff unavailable: {type(exc).__name__})", flush=True)
            # Production binds the scan to a head verified before and after the
            # fetch; the bench's workspace is the fork, so the PR must still be there.
            bound = head_sha if head_sha and pr_head == head_sha else None
            if raw_diff is not None and head_sha and pr_head != head_sha:
                raise BenchSetupError(
                    f"fixture PR {pr_url} head {str(pr_head)[:8]} is not the forked "
                    f"'{task.branch}' head {head_sha[:8]}: point --prs at the PR for that branch")
            extra = pr_card_context(task.config_key, raw_diff, files, bound, workflow=workflow)
    except BaseException:
        for cleanup in cleanups:
            with contextlib.suppress(Exception):
                cleanup()
        raise
    context = build_card_context(
        task, backend=backend, repo_url=repo_url, branch=branch, model=model,
        model_block=model_block, orchestration=orchestration, workflow=workflow,
        workflow_env=workflow_env, tuning=tuning, pr_url=pr_url, persona=persona, extra=extra)
    return PreparedCell(context, cleanups=cleanups)


def _container_env(endpoint: dict, dotenv: dict, task: RoleTask, backend: str) -> list[str]:
    """``docker run -e`` pairs: the endpoint's env, the GitHub token, bench flags.

    A role workflow (the advocate) reaches its model through the gateway
    (``LITELLM_PROXY_*``); when the endpoint env does not set them the bench uses
    the inference gateway from ``.env``, as an operator would."""
    cenv = {k: _expand(str(v), dotenv) for k, v in (endpoint.get("env", {}) or {}).items()}
    if task.dispatch == "intake":
        cenv.setdefault("LITELLM_PROXY_BASE_URL", str(dotenv.get("COORDINARE_INFERENCE_BASE_URL") or ""))
        cenv.setdefault("LITELLM_PROXY_AUTH_TOKEN", str(dotenv.get("LITELLM_MASTER_KEY") or "").strip())
    docker_env: list[str] = []
    for k, v in cenv.items():
        docker_env += ["-e", f"{k}={v}"]
    docker_env += ["-e", f"GITHUB_TOKEN={_gh_token(dotenv)}",
                   "-e", "PERFORMER_SECRET_SOURCE_INIT_PAYLOAD=1",
                   "-e", "PERFORMER_SECRET_SOURCE_ENV=1",
                   # keep the workspace so post-job graders can inspect committed
                   # plan files / test results / docs (the job loop would
                   # otherwise tear the stand down before we read it).
                   "-e", "PERFORMER_KEEP_STAND=1"]
    key_name = {"codex": "OPENAI_API_KEY", "claude_code": "ANTHROPIC_AUTH_TOKEN"}.get(
        backend.replace("-", "_"))
    if key_name and (master := (dotenv.get("LITELLM_MASTER_KEY") or "").strip()):
        docker_env += ["-e", f"{key_name}={master}"]
    return docker_env


# ---------------------------------------------------------------------------
# Dispatch one (task, endpoint) and grade it.
# ---------------------------------------------------------------------------
def run_cell(task: RoleTask, endpoint: dict, cfg: dict, dotenv: dict,
             repo_url: str, prs: dict, judge_cfg: dict | None, outdir: Path,
             config_path: str | None = None) -> dict:
    eid = endpoint["id"]
    backend = (endpoint.get("env", {}) or {}).get("BACKEND", "")
    # 080: resolve the dispatch model + orchestration from the role's mode,
    # falling back to the legacy inline model for pre-080 configs.
    _dispatch = resolve_role_dispatch(config_path or "", task.config_key, cfg=cfg,
                                      backend=backend, dotenv=dotenv)
    model = _dispatch["model"] or _backend_model(cfg, endpoint)
    _dispatch["model"] = model
    orchestration = _dispatch["orchestration"]
    res = {"role": task.label, "endpoint": eid, "backend": backend, "model": model,
           "strategy": (orchestration or {}).get("strategy", "single"),
           "category": "ERROR", "status": None, "markers": [], "detail": "",
           "judge_correct": None, "judge_quality": None, "judge_reason": "",
           "error_code": None, "state": None, "log_file": None, "raw_file": None,
           "reconciled": None}
    # Hoisted so the finally block can persist the full failure context.
    status: dict = {}
    result: dict = {}
    summary = ""
    transcript: list[dict] = []
    prepared: PreparedCell | None = None
    port = _free_port()
    name = f"bench-{task.label}-{uuid.uuid4().hex[:6]}"
    gh_token = _gh_token(dotenv)
    docker_env = _container_env(endpoint, dotenv, task, backend)
    volume = None
    if task.dispatch == "bootstrap":
        # The bootstrap writes into its cache mount; a named volume stands in
        # for the devenv claim and goes with the container.
        volume = f"{name}-devenv"
        docker_env += ["-v", f"{volume}:{BOOTSTRAP_CACHE_PATH}"]
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
        prod_persona = _prod_persona(task.config_key, config_path)
        if prod_persona:
            res["persona_source"] = "production"
        try:
            prepared = prepare_cell(
                task, backend=backend, repo_url=repo_url, gh_token=gh_token, dispatch=_dispatch,
                persona=prod_persona, prs=prs, dotenv=dotenv, judge_cfg=judge_cfg,
                config_path=config_path)
        except BenchSetupError as exc:
            res["detail"] = str(exc)
            res["category"] = "FAIL_HARNESS"
            return res
        payload = build_job_payload(prepared.card_context, gh_token=gh_token, dotenv=dotenv)
        job_id = payload["job_id"]
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
        transcript = fetch_transcript(base, job_id)
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
            cid=cid, repo_subdir=repo_subdir, fixture=prepared.fixture)
        contract_ok, markers, detail = task.grader(ctx)
        res.update(status=ctx.status, markers=markers, detail=detail)

        # Categorise: harness vs model vs pass.
        blob = f"{summary} {result.get('error_code','')} {ctx.status}"
        jc = jq = None
        jr_reason = ""
        if judge_cfg and _judge_eligible(ctx):
            jc, jq, jr_reason = judge(task, ctx, detail, **judge_cfg)
            res.update(judge_correct=jc, judge_quality=jq, judge_reason=jr_reason)
        category, harness_hit = categorize(ctx.state, blob, contract_ok, markers, jc)
        res["category"] = category
        res["state"] = ctx.state
        res["error_code"] = result.get("error_code")
        if harness_hit:
            res["detail"] = f"{detail} | harness:{','.join(harness_hit)}"
        # FR-009: surface (never swallow) a failed/error → PASS reconciliation.
        note = reconciliation_note(ctx.state, result.get("error_code"), category)
        if note:
            res["reconciled"] = note
            res["detail"] = f"{res['detail']} | {note}"
        return res
    except Exception as exc:
        res["detail"] = f"{type(exc).__name__}: {exc}"[:160]
        return res
    finally:
        # Tear down per-run fixtures (throwaway branch, advocate issues) so the
        # shared fixtures stay pristine and nothing accumulates on the remote.
        for cleanup in (prepared.cleanups if prepared else []):
            with contextlib.suppress(Exception):
                cleanup()
        if cid:
            # Persist the cell for every run (391): the job result, the
            # performer's logs_excerpt and, beside it, the raw transcript.
            # Container stderr (where crashes / tracebacks surface) is kept for
            # every non-PASS cell and for a reconciled-to-PASS cell (FR-009).
            container_logs = ""
            if res["category"] != "PASS" or res.get("reconciled"):
                with contextlib.suppress(Exception):
                    clogs = subprocess.run(["docker", "logs", "--tail", "400", cid],
                                           capture_output=True, text=True, timeout=20)
                    container_logs = ((clogs.stdout or "") + (clogs.stderr or ""))[-8000:]
            with contextlib.suppress(Exception):
                cell_rel, raw_rel = write_cell_files(outdir, task.label, eid, {
                    "row": res,
                    "state": status.get("state"),
                    "error_code": result.get("error_code"),
                    "logs_excerpt": result.get("logs_excerpt"),
                    "summary": summary,
                    "container_logs": container_logs,
                }, transcript)
                res["log_file"], res["raw_file"] = cell_rel, raw_rel
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        if volume:
            subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True, timeout=30)


def select_tasks(tasks: list[RoleTask], roles: str | None) -> list[RoleTask]:
    """``--roles`` picks tasks by label or by performer key (391), so
    ``--roles tech_writer`` runs both tech_writer tasks."""
    if not roles:
        return list(tasks)
    want = {r.strip() for r in roles.split(",") if r.strip()}
    return [t for t in tasks if t.label in want or t.config_key in want]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="bench repo clone URL")
    ap.add_argument("--prs", help="json file mapping {role: pr_url}")
    ap.add_argument("--config", default=str(REPO_ROOT / "config.yaml"))
    ap.add_argument("--roles", help="comma list of role labels or performer keys (default: all)")
    ap.add_argument("--backends", help="comma list of endpoint ids (default: all)")
    ap.add_argument("--judge-model", help="LiteLLM model for the judge, e.g. local/gpt-oss:120b")
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
    tasks = select_tasks(TASKS, args.roles)

    judge_cfg = None
    if args.judge_model:
        base = (dotenv.get("COORDINARE_INFERENCE_BASE_URL") or
                "https://litellm.example/v1")
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
        r = run_cell(t, e, cfg, dotenv, args.repo, prs, judge_cfg, outdir,
                     config_path=args.config)
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
            mdl = (r.get("model") or "").replace("local/", "")
            cells.append(f"{short.get(r['category'], '?')} {mdl}")
        md.append(f"| {t.label} | " + " | ".join(cells) + " |")
    md += ["", "## Per-persona — which (backend, model) PASSed", ""]
    for t in tasks:
        winners = [f"{r['endpoint'].replace('-ephemeral','')}({(r.get('model') or '').replace('local/','')})"
                   for r in results if r["role"] == t.label and r["category"] == "PASS"]
        md.append(f"- **{t.label}**: {', '.join(winners) if winners else '— none passed —'}")
    md += ["", "## Totals", "", ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))]
    (outdir / "results.md").write_text("\n".join(md) + "\n")
    print(f"Full results: {outdir / 'results.json'}  +  {outdir / 'results.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
