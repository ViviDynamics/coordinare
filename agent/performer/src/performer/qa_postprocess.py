"""QA post-processing: turn the QA backend's report into a PerformerResponse.

Extracted from ``main.handle_status`` (spec 164 T049). The QA role had grown 498
lines of post-hoc parsing inside a 4,300-line file, unreachable by any unit test
except by running the whole handler. It is now one function with the QA-only
helpers beside it. Behaviour is unchanged: the body of ``finalize_qa`` is the
former branch verbatim.

This module is LOAD-BEARING for both QA paths -- the legacy single-backend run
and the spec-164 workflow (its adapter surfaces the report as
``backend_status.output``, which lands here). Extract, never delete.

Collaborators (git, GitHub, capture, upload) and shared helpers are looked up on
``performer.main`` at call time rather than imported by name. Two reasons: it
avoids an import cycle (main imports this module), and the test suite patches
those collaborators on ``performer.main`` -- ~85 sites -- which a module-level
import here would silently bypass, letting tests reach real git and GitHub.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import structlog

# Safe: performer.main imports this module lazily (inside handle_status), so
# importing main here does not form a cycle. _VISUAL_TASK_KEYWORDS is shared
# with main's own persona logic and stays there.
from performer.main import _VISUAL_TASK_KEYWORDS
from performer.models import Score
from performer.protocol import PerformerResponse

log = structlog.get_logger(__name__)


async def finalize_qa(perf, backend_status, settings) -> PerformerResponse:
    """Finalize a QA run. Body is the former ``handle_status`` QA branch, verbatim."""
    from performer import main as _m  # late: main imports this module

    _extract_json = _m._extract_json
    _extract_pr_number = _m._extract_pr_number
    _handle_backend_parse_failure = _m._handle_backend_parse_failure
    _doc_folder = _m._doc_folder
    commit_file = _m.commit_file
    get_head_sha = _m.get_head_sha
    post_pr_comment = _m.post_pr_comment
    post_issue_comment = _m.post_issue_comment
    resolve_visual_evidence_urls = _m.resolve_visual_evidence_urls
    boot_and_capture_app_screenshot = _m.boot_and_capture_app_screenshot

    qa_raw = backend_status.output or ""
    qa_output = (
        _extract_json(qa_raw) if isinstance(qa_raw, str) and qa_raw.strip() else None
    )
    if not isinstance(qa_output, dict):
        # 088 US6 (FR-013): the fold-in below never runs on this path, so
        # consult the recorded services-start failure HERE too. An agent
        # that could not reach the database is MORE likely to emit
        # degenerate output, not less — and treating that as a malformed
        # backend response loses the env blocker, skips the cache
        # invalidation, and burns the bounded malformed-output retries
        # against the same broken cache. A recorded env blocker is the
        # better explanation for unusable output, so it wins.
        from performer.workspace import consume_services_start_failure
        services_start_failure = consume_services_start_failure()
        if services_start_failure:
            log.warning(
                "qa.env_blocked_unparseable_output",
                session_id=perf.session_id,
                env_error=services_start_failure[:200],
                output_preview=qa_raw[:200],
            )
            return _env_blocked_qa_response(perf, services_start_failure)
        reason = (
            "was empty" if not qa_raw.strip()
            else "could not be parsed as a JSON object"
        )
        return await _handle_backend_parse_failure(
            perf, qa_raw, "QA", settings, reason,
        )

    # Commit new test files written by the backend (FR-005)
    new_tests = qa_output.get("new_test_files", [])
    if isinstance(new_tests, list):
        for tf in new_tests:
            if isinstance(tf, dict) and tf.get("path") and "content" in tf:
                try:
                    await commit_file(perf.stand, tf["path"], tf["content"],
                                      "test: add QA acceptance criterion tests")
                    perf.qa_new_tests.append(tf["path"])
                except Exception as exc:
                    log.error("qa_test_commit_failed", path=tf.get("path"), error=str(exc))
                    # This exit happens before resolve ran, so it must make
                    # the same evidence-tree cleanup promise (411 round-four
                    # review).
                    _delete_qa_capture_dir(qa_output)
                    perf.state = "error"
                    perf.error_reason = f"Failed to commit new test file {tf.get('path')}: {exc}"
                    return PerformerResponse(
                        status="error", session_id=perf.session_id,
                        reason=perf.error_reason,
                    )

    verification_steps = _normalise_steps(qa_output.get("verification_steps", []))
    demo_steps = _normalise_steps(qa_output.get("demo_steps", []))
    for step in demo_steps:
        if step not in verification_steps:
            verification_steps.append(step)
    if not verification_steps:
        verification_steps = _default_verification_steps(perf.score)
    pre_fix_repro_steps = _normalise_steps(
        qa_output.get("pre_fix_repro_steps", qa_output.get("reproduction_steps", [])),
    )
    demo_setup_steps = _normalise_steps(qa_output.get("demo_setup_steps", []))
    visual_capture_commands = _normalise_steps(qa_output.get("visual_capture_commands", []))
    visual_capture_blockers = _normalise_steps(qa_output.get("visual_capture_blockers", []))
    visual_evidence = _normalise_visual_evidence(qa_output.get("visual_evidence", []))
    visual_validation_required = _qa_visual_validation_required(
        score=perf.score,
        qa_output=qa_output,
        demo_setup_steps=demo_setup_steps,
        visual_capture_blockers=visual_capture_blockers,
        visual_evidence=visual_evidence,
    )

    # Deterministic screenshot backstop (performer-owned): when a visual
    # change needs evidence but the QA agent produced no on-disk artifact,
    # capture it ourselves (system python + Playwright) — removes the LLM's
    # browser-driving variance (wrong interpreter / Selenium / faked path).
    # If the app isn't already serving, infer the project's start command
    # and boot it in the activated env-cache (cwd=workspace), then tear it
    # back down. Never fabricates (returns a verified on-disk file only).
    if visual_validation_required and not _has_local_visual_artifact(visual_evidence):
        # Same layering the workflow boots with (os env, env cache, then the
        # operator's workflow_env last and highest — qa/__init__ builds
        # boot_env identically). Without the workflow_env layer a capture
        # after a workflow_env override cannot boot the same app (411 review).
        _cap_env = {
            **os.environ,
            **(getattr(perf.stand, "cache_env", None) or {}),
            **(getattr(perf.score, "workflow_env", None) or {}),
        }
        # The workflow's shape reading rides in the report: the capture boots
        # with the same command the workflow would have used (override wins
        # inside resolve_start_command), falling back to the framework
        # heuristics only when there is no reading (411 review).
        _shape_cmd = str(qa_output.get("app_start_command") or "").strip()
        _auto_shot = boot_and_capture_app_screenshot(
            env=_cap_env,
            workspace=perf.stand.path,
            shape=SimpleNamespace(start_command=_shape_cmd) if _shape_cmd else None,
        )
        if _auto_shot:
            visual_evidence.append({
                "label": "coordinare auto-capture",
                "kind": "screenshot",
                "path_or_url": _auto_shot,
                "note": "captured by the performer against the running app",
            })
            log.info(
                "qa_capture.injected",
                card_id=str(getattr(perf.score, "card_id", "") or ""),
                path=_auto_shot,
            )

    # Check for failures/env blockers before summarising and posting evidence.
    raw_failures = qa_output.get("failures", [])
    failures = [f for f in (raw_failures if isinstance(raw_failures, list) else []) if isinstance(f, dict)]
    env_error = str(qa_output.get("environment_error", "")).strip()
    # 088 US6 (FR-013): fold any coordinare-recorded env-cache
    # services-start failure (e.g. Postgres could not start without a
    # password) into the environment_error channel. The agent's own QA
    # JSON may omit a setup-time failure it never saw; without this the
    # blocker is lost and a zero-evidence pass misclassifies as an
    # unsubstantiated FAILED (a code defect) instead of qa_env_blocked
    # (a held environment blocker the coordinare repairs, not the code).
    from performer.workspace import consume_services_start_failure
    services_start_failure = consume_services_start_failure()
    if services_start_failure:
        env_error = (
            f"{env_error}\n{services_start_failure}".strip()
            if env_error
            else services_start_failure
        )
    failures.extend(
        _qa_visual_evidence_failures(
            required=visual_validation_required,
            stand_path=perf.stand.path,
            visual_capture_commands=visual_capture_commands,
            visual_capture_blockers=visual_capture_blockers,
            visual_evidence=visual_evidence,
        ),
    )
    latest_main_sha = perf.score.latest_main_sha.strip()
    qa_freshness_check: dict = {}
    if not latest_main_sha:
        # Coordinare did not supply latest_main_sha — cannot verify freshness.
        # Record the indeterminate state but do NOT add a failure: QA can still
        # pass when coordinare hasn't yet populated last_known_main_sha (e.g. first
        # cycle after startup).  This is a non-blocking indeterminate.
        qa_freshness_check = {
            "latest_main_sha": None,
            "branch_head_sha": None,
            "up_to_date": None,
            "detail": "freshness_check_indeterminate",
        }
    else:
        try:
            branch_head: str | None = None
            try:
                branch_head = await get_head_sha(perf.stand)
            except Exception as _head_exc:
                log.debug("qa.branch_head_fetch_failed", error=str(_head_exc))
            _proc = await asyncio.create_subprocess_exec(
                "git", "merge-base", "--is-ancestor", latest_main_sha, "HEAD",
                cwd=str(perf.stand.path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                _rc = await asyncio.wait_for(_proc.wait(), timeout=30)
            except asyncio.TimeoutError:
                _proc.kill()
                await _proc.wait()
                raise
            if _rc == 0:
                qa_freshness_check = {
                    "latest_main_sha": latest_main_sha,
                    "branch_head_sha": branch_head,
                    "up_to_date": True,
                    "detail": "branch includes latest main (git merge-base confirmed)",
                }
            elif _rc == 1:
                # Exit code 1: SHA is not an ancestor (branch is behind main).
                qa_freshness_check = {
                    "latest_main_sha": latest_main_sha,
                    "branch_head_sha": branch_head,
                    "up_to_date": False,
                    "detail": f"{latest_main_sha[:8]} is not an ancestor of HEAD",
                }
                failures.append({
                    "file": None,
                    "line": None,
                    "message": "Branch is behind latest main — rebase required before QA can pass",
                    "type": "freshness",
                    "criterion": "Branch includes latest main",
                    "expected": f"Branch rebased onto {latest_main_sha[:8]}",
                    "actual": "Branch is behind latest main — rebase required",
                })
            else:
                # Exit codes > 1 indicate a git/environment error, not a
                # definitive "behind main" result — treat as indeterminate.
                raise RuntimeError(f"git merge-base exited with code {_rc}")
        except Exception as _exc:
            qa_freshness_check = {
                "latest_main_sha": None,
                "branch_head_sha": None,
                "up_to_date": None,
                "detail": "freshness_check_indeterminate",
            }
            failures.append({
                "file": None,
                "line": None,
                "message": "Branch freshness could not be verified — environment/git failure",
                "type": "freshness_indeterminate",
                "criterion": "Branch freshness check",
                "expected": "Freshness check succeeds",
                "actual": "Branch freshness could not be verified — environment/git failure",
            })
            log.warning("qa.freshness_check_failed", error=str(_exc))

    # 077 (pipeline-tolerant): split environmental incapability (couldn't
    # run tests / no browser / no DB / missing binary) from real code
    # defects. QA blocks ONLY on defects it actually found; environmental
    # limits are advisory so a card isn't trapped by a container that
    # can't verify it. A FAILED verdict must mean "checked and broken",
    # not "couldn't check".
    defect_failures = [f for f in failures if not _qa_failure_is_environmental(f)]
    env_limited = bool(env_error) or len(defect_failures) < len(failures)
    qa_passed_flag = not defect_failures

    criteria_checked = qa_output.get("criteria_checked", 0)
    criteria_passed = qa_output.get("criteria_passed", 0)

    # 083: refuse a claimed pass that rests on self-report alone. A model
    # that asserts criteria_passed>0 with failures=[] but ran nothing
    # (no executed_checks, no committed tests, no captured proof) is
    # rubber-stamping — synthesise a real defect so the gate FAILs
    # instead of waving the PR through. Phrase the failure to avoid the
    # environmental regex so it counts as a defect, not advisory.
    # 088 (FR-001/FR-002): env-limited runs are no longer EXEMPT from the
    # evidence check — a zero-evidence pass claim under an environment
    # blocker classifies as the terminal status qa_env_blocked instead
    # (the PR #159 false-pass), never an unflagged clean PASS.
    executed_checks = _qa_execution_evidence(qa_output)
    # 088 (FR-005): visual/UI criteria need app-boot proof. Without a
    # zero-exit boot check referenced in executed_checks, the model's
    # screenshots cannot back the claim — they drop out of the evidence
    # count, folding into the unsubstantiated/cross-validation gates.
    app_boot_check, app_boot_ok = _qa_app_boot_evidence(qa_output, executed_checks)
    evidence_visual = [ev for ev in visual_evidence if ev.get("path_or_url")]
    if visual_validation_required and not app_boot_ok:
        evidence_visual = []
    evidence_count = (
        len(executed_checks) + len(perf.qa_new_tests) + len(evidence_visual)
    )
    qa_env_blocked = False
    if _qa_unsubstantiated_pass(
        qa_passed_flag=qa_passed_flag,
        criteria_passed=criteria_passed,
        executed_checks=executed_checks,
        new_tests=perf.qa_new_tests,
        visual_evidence=evidence_visual,
    ):
        if env_limited:
            # 'Couldn't verify' stays honest — but it is a distinct
            # terminal outcome now, not a pass.
            qa_env_blocked = True
            log.warning(
                "qa.env_blocked_zero_evidence",
                session_id=perf.session_id,
                criteria_passed=criteria_passed,
                env_error=(env_error or "")[:200],
            )
        else:
            unsubstantiated = {
                "type": "unsubstantiated_pass",
                "criterion": "Execution evidence required for a QA pass",
                "expected": (
                    "Verification checks actually executed with commands and "
                    "exit codes recorded, committed tests, or captured proof."
                ),
                "actual": (
                    "Model reported criteria as passing but supplied zero "
                    "execution evidence (zero checks executed, zero tests "
                    "committed, zero captured proof). Unsubstantiated pass refused."
                ),
            }
            failures.append(unsubstantiated)
            defect_failures.append(unsubstantiated)
            qa_passed_flag = False
            log.warning(
                "qa.unsubstantiated_pass_refused",
                session_id=perf.session_id,
                criteria_passed=criteria_passed,
            )

    # 120 (US1): an env-limited advisory pass that verified ZERO criteria
    # is "couldn't verify", not a pass — route it to qa_env_blocked (HOLD
    # for cache repair) instead of the advisory qa_passed below.
    if not qa_env_blocked and _qa_env_limited_without_verification(
        qa_passed_flag=qa_passed_flag,
        env_limited=env_limited,
        criteria_checked=criteria_checked,
        criteria_passed=criteria_passed,
    ):
        qa_env_blocked = True
        log.warning(
            "qa.env_blocked_zero_criteria_passed",
            session_id=perf.session_id,
            criteria_checked=criteria_checked,
            env_error=(env_error or "")[:200],
        )

    # 411 (AC1): the workflow's own verdict is evidence, not a suggestion.
    # report["passed"] False means the model executed its checks and found the
    # change wanting; the environmental classifier above cannot reclassify a
    # checked-and-broken verdict into an advisory pass. Only a MISSING
    # environment (no baseline to compare, no server to exercise) routes the
    # refusal to qa_env_blocked instead.
    workflow_passed = qa_output.get("passed")
    wf_raw = qa_output.get("qa_findings")
    wf_findings = (
        [f for f in wf_raw if isinstance(f, dict)]
        if isinstance(wf_raw, list) else []
    )
    wf_defect = any(
        not _qa_failure_is_environmental(f) for f in wf_findings
    )
    if workflow_passed is False:
        # Round-eight review: the workflow's own non-environmental findings
        # belong in the payload whenever the workflow failed, even when the
        # postprocessor already carries a defect — the verdict is failed
        # either way, and dropping the findings would throw away the
        # implementer's actionable evidence. Deduplicated against the
        # payload so a workflow echo of an already-recorded failure does
        # not double-report it.
        seen = {
            (str(f.get("type", "")), str(f.get("criterion", "")),
             str(f.get("expected", "")), str(f.get("actual", "")))
            for f in defect_failures
        }
        for f in wf_findings:
            if _qa_failure_is_environmental(f):
                continue
            defect = {
                "type": str(f.get("category", "workflow_finding")),
                "criterion": str(f.get("criterion") or "QA workflow finding"),
                "expected": str(f.get("expected", "")),
                "actual": str(f.get("observed") or f.get("actual", "")),
            }
            key = (defect["type"], defect["criterion"],
                   defect["expected"], defect["actual"])
            if key in seen:
                continue
            seen.add(key)
            failures.append(defect)
            defect_failures.append(defect)
    if workflow_passed is False and qa_passed_flag:
        # Round-five review: env_limited rides the postprocessor's failures
        # list, which also holds synthetic capture failures. The workflow's
        # OWN findings are the verdict's evidence — a hard finding there
        # (an unexpected regression, an unobservable step) is
        # checked-and-broken, not "couldn't verify", so it must reach
        # qa_failed even when capture is also unavailable.
        if env_limited and not wf_defect:
            qa_env_blocked = True
            log.warning(
                "qa.env_blocked_workflow_failed",
                session_id=perf.session_id,
                env_error=(env_error or "")[:200],
            )
        else:
            # This branch runs only when the report's failures payload is
            # defect-free, so the workflow's own non-environmental findings
            # are detail the payload lacks — surface them so the implementer
            # gets the actionable regression, not just the generic verdict
            # refusal (round-seven review).
            categories = [
                str(f.get("category", "")) for f in wf_findings if f.get("category")
            ]
            workflow_failure = {
                "type": "workflow_verdict",
                "criterion": "QA workflow verdict",
                "expected": "the QA workflow's own verdict is passed",
                "actual": "the QA workflow reported the run as FAILED (findings: "
                          + (", ".join(categories) or "none recorded") + ")",
            }
            failures.append(workflow_failure)
            defect_failures.append(workflow_failure)
            qa_passed_flag = False
            log.warning(
                "qa.workflow_verdict_refused",
                session_id=perf.session_id,
                findings=", ".join(categories) or "none",
            )

    # 411 (AC7): a pass with zero criteria checked demonstrates nothing.
    # Defence in depth — the workflow refuses an empty plan itself; this gate
    # catches any build that still reports passed=True with nothing checked.
    # criteria_checked arrives from unvalidated model JSON: coerce safely the
    # way the response path does, or a value like "unknown" would raise and
    # take the whole QA response with it (411 round-three review). Non-finite
    # floats are valid JSON numbers (NaN/Infinity) that int() also rejects —
    # they coerce to 0, refusing the pass rather than crashing the response
    # (411 round-six review).
    _checked = _qa_safe_int(criteria_checked)
    if (
        workflow_passed is True
        and not qa_env_blocked
        and qa_passed_flag
        and _checked == 0
    ):
        zero_criteria = {
            "type": "zero_criteria",
            "criterion": "Zero acceptance criteria checked",
            "expected": "at least one acceptance criterion checked and passed",
            "actual": "The QA run passed with zero acceptance criteria checked: "
                      "no acceptance criteria were stated, so nothing was "
                      "demonstrated. Zero-criteria passes are refused.",
        }
        failures.append(zero_criteria)
        defect_failures.append(zero_criteria)
        qa_passed_flag = False
        log.warning("qa.zero_criteria_pass_refused", session_id=perf.session_id)

    # Bug 16.2: upload any container-local screenshots to GitHub's
    # user-attachments CDN so the PR/issue comment renders embeddable
    # images instead of container-local /tmp/... paths. This runs BEFORE the
    # committed qa.md is built: the report records the published URLs, not
    # container-local paths the cleanup below then deletes (411 round-seven
    # review).
    try:
        owner_for_upload, repo_for_upload = perf.score.owner_repo
        upload_issue_no = perf.score.issue_number or _extract_pr_number(perf.pr_url)
        visual_evidence = await resolve_visual_evidence_urls(
            visual_evidence,
            workspace_root=perf.stand.path,
            github_token=perf.score.effective_github_token,
            org=owner_for_upload,
            repo=repo_for_upload,
            issue_number=upload_issue_no,
        )
    except Exception as exc:
        log.warning("qa.visual_evidence_upload_failed", error=str(exc))

    # Managed lifecycle for the evidence dir (411 review): resolve is the last
    # consumer — it has replaced the container-local paths with CDN URLs (or
    # recorded why it could not) — so the workflow's qa-visual-* tree is
    # deleted here instead of accumulating in /tmp for the life of the
    # performer. The helper validates the path is workflow-owned before
    # removing anything; failure to clean is never a verdict issue.
    _delete_qa_capture_dir(qa_output)

    # Commit QA report to the architecture folder
    folder = _doc_folder(perf.score)
    qa_report_content = f"# QA Report: {perf.score.title}\n\n"
    if qa_env_blocked:
        _result_text = "ENVIRONMENT-BLOCKED — UNVERIFIED"
    else:
        _result_text = "PASSED" if qa_passed_flag else "FAILED"
    qa_report_content += f"**Result: {_result_text}**\n\n"
    qa_report_content += f"- Criteria checked: {criteria_checked}\n"
    qa_report_content += f"- Criteria passed: {criteria_passed}\n"
    if perf.qa_new_tests:
        qa_report_content += f"- New tests added: {len(perf.qa_new_tests)}\n"
        for tp in perf.qa_new_tests:
            qa_report_content += f"  - `{tp}`\n"
    if pre_fix_repro_steps:
        qa_report_content += "\n## Known Reproduction Steps (pre-fix context)\n\n"
        for idx, step in enumerate(pre_fix_repro_steps, start=1):
            qa_report_content += f"{idx}. {step}\n"
    if demo_setup_steps:
        qa_report_content += "\n## Visual Capture Setup Steps\n\n"
        for idx, step in enumerate(demo_setup_steps, start=1):
            qa_report_content += f"{idx}. {step}\n"
    if visual_capture_commands:
        qa_report_content += "\n## Visual Capture Commands Attempted\n\n"
        for idx, command in enumerate(visual_capture_commands, start=1):
            qa_report_content += f"{idx}. `{command}`\n"
    qa_report_content += "\n## Verification Steps\n\n"
    for idx, step in enumerate(verification_steps, start=1):
        qa_report_content += f"{idx}. {step}\n"
    qa_report_content += "\n## Visual Evidence\n\n"
    if visual_evidence:
        for ev in visual_evidence:
            label = ev.get("label", "Evidence")
            kind = ev.get("kind", "artifact")
            loc = ev.get("path_or_url", "")
            note = ev.get("note", "")
            qa_report_content += f"- **{label}** ({kind})"
            if loc and _looks_like_url(loc):
                qa_report_content += f": `{loc}`"
            elif loc:
                # The entry failed to publish — resolve left the
                # container-local qa-visual-* path, and the evidence tree is
                # deleted immediately after — so the committed report names
                # the failure rather than a dead local path (411 round-eight
                # review).
                qa_report_content += " (upload failed; local capture not retained)"
            if note:
                qa_report_content += f" — {note}"
            qa_report_content += "\n"
    else:
        qa_report_content += "- No visual artifacts captured in this QA run.\n"
        if visual_capture_blockers:
            qa_report_content += "\n### Capture blockers\n\n"
            for blocker in visual_capture_blockers:
                qa_report_content += f"- {blocker}\n"
    if failures:
        qa_report_content += "\n## Failures\n\n"
        for f in failures:
            criterion = f.get("criterion", "")
            expected = f.get("expected", "")
            actual = f.get("actual", "")
            qa_report_content += f"- **{criterion}**\n  Expected: {expected}\n  Actual: {actual}\n\n"
    try:
        issue_num = perf.score.issue_number
        await commit_file(
            perf.stand, f"{folder}/qa.md", qa_report_content,
            f"chore: add QA report for #{issue_num}" if issue_num else "chore: add QA report",
        )
    except Exception as exc:
        log.warning("qa.commit_report_failed", error=str(exc))

    qa_comment = _build_qa_pr_comment(
        score=perf.score,
        passed=qa_passed_flag,
        criteria_checked=_qa_safe_int(criteria_checked),
        criteria_passed=_qa_safe_int(criteria_passed),
        failures=failures,
        verification_steps=verification_steps,
        pre_fix_repro_steps=pre_fix_repro_steps,
        demo_setup_steps=demo_setup_steps,
        visual_capture_commands=visual_capture_commands,
        visual_capture_blockers=visual_capture_blockers,
        visual_evidence=visual_evidence,
        environment_error=env_error,
        evidence_count=evidence_count,
        env_blocked=qa_env_blocked,
    )

    # Post QA evidence to the PR so humans can quickly validate behavior.
    pr_number = _extract_pr_number(perf.pr_url)
    if pr_number > 0:
        owner, repo = perf.score.owner_repo
        token = perf.score.effective_github_token
        try:
            await post_pr_comment(owner, repo, pr_number, body=qa_comment, token=token)
        except Exception as exc:
            log.warning("qa.evidence_comment_failed", error=str(exc), exc_info=True)
    else:
        log.warning("qa.evidence_comment_skipped_missing_pr_url", pr_url=perf.pr_url)

    issue_number = perf.score.issue_number
    if issue_number > 0:
        owner, repo = perf.score.owner_repo
        token = perf.score.effective_github_token
        try:
            await post_issue_comment(owner, repo, issue_number, body=qa_comment, token=token)
        except Exception as exc:
            log.warning(
                "qa.evidence_issue_comment_failed",
                issue_number=issue_number,
                error=str(exc),
                exc_info=True,
            )

    # 077: env_error and environmental failures are ADVISORY — they no
    # longer hard-block QA. The lifecycle only stops for real defects
    # (defect_failures). If QA couldn't verify due to the container's
    # limits, it passes DEGRADED with the limitation recorded —
    # 088: UNLESS the run produced zero evidence behind its pass claim,
    # in which case it is qa_env_blocked (couldn't verify ≠ verified).
    if not defect_failures:
        perf.qa_report = {
            "criteria_checked": qa_output.get("criteria_checked", 0),
            "criteria_passed": qa_output.get("criteria_passed", 0),
            "executed_checks": executed_checks,
            "evidence_count": evidence_count,
            "app_boot_check": app_boot_check,
            "env_limited": env_limited,
            "environment_error": env_error or None,
            "new_tests_added": len(perf.qa_new_tests),
            "verification_steps": verification_steps,
            "pre_fix_repro_steps": pre_fix_repro_steps,
            "demo_setup_steps": demo_setup_steps,
            "visual_capture_commands": visual_capture_commands,
            "visual_capture_blockers": visual_capture_blockers,
            "visual_evidence": visual_evidence,
            "visual_validation_required": visual_validation_required,
        }
        if qa_freshness_check:
            perf.qa_report["qa_freshness_check"] = qa_freshness_check
        if qa_env_blocked:
            perf.state = "qa_env_blocked"
            return PerformerResponse(
                status="qa_env_blocked",
                session_id=perf.session_id,
                reason=env_error or (
                    "environment-blocked: pass claim with zero execution evidence"
                ),
                report=perf.qa_report,
            )
        if env_limited:
            log.warning(
                "qa.env_limited_advisory_pass",
                session_id=perf.session_id,
                env_error=(env_error or "")[:200],
                env_failure_count=len(failures) - len(defect_failures),
            )
        perf.state = "qa_passed"
        return PerformerResponse(
            status="qa_passed",
            session_id=perf.session_id,
            report=perf.qa_report,
        )

    # Real code defects exist → changes_requested / block (as before).
    # Only defect_failures count; environmental limits were advisory above.
    max_cycles = settings.QA_MAX_CYCLES if settings else 3
    perf.qa_cycle += 1
    if perf.qa_cycle >= max_cycles:
        summary = f"QA: {len(defect_failures)} acceptance criterion failure(s) after {perf.qa_cycle} fix attempt(s)"
        perf.state = "blocked"
        perf.open_questions = [summary]
        return PerformerResponse(
            status="blocked",
            session_id=perf.session_id,
            questions=[summary],
        )
    perf.qa_failures = [f for f in defect_failures if isinstance(f, dict)]
    perf.state = "qa_failed"
    _fail_report: dict | None = {"qa_freshness_check": qa_freshness_check} if qa_freshness_check else None
    return PerformerResponse(
        status="qa_failed",
        session_id=perf.session_id,
        failures=perf.qa_failures,
        report=_fail_report,
    )


# --- shared helpers, delegated to performer.main --------------------------------

def _persona_tag(*a, **k):
    """Lazy delegate to performer.main (shared; cycle + test patch targets)."""
    from performer import main as _m

    return _m._persona_tag(*a, **k)

# --- QA-only constants and predicates, moved verbatim from main.py -----------------

_QA_ENV_FAILURE_PATTERNS = re.compile(
    r"pg::|connectionbad|could not connect|connection refused|econnrefused|"
    r"read-?only file system|sqlite3|not on \$?path|command not found|not installed|"
    r"no such file|headless|chromium|chrome|browser|display|selenium|webdriver|"
    r"postgres|pg_ctl|initdb|database (is )?(unavailable|not running|down)|"
    r"no screenshot|no artifacts|visual artifacts|could not (start|launch|run)|"
    # Natural "couldn't check" phrasings real models emit (captured verbatim from
    # PR ViviDynamics/website#159). Without these, an honest "I couldn't run it"
    # slips past the regex, counts as a defect, and wrongly blocks the card —
    # the opposite of 077's intent ("FAILED" = checked and broken, not couldn't check).
    r"could not be (executed|run|started|verified|completed|built|installed)|"
    r"can(not|'?t)\s+(be\s+)?(verif|execut|run|start|launch|test|complet|build|install)|"
    r"unable to (verify|execute|run|start|launch|test|complete|build|install)|"
    r"fails? to start|failed to start|"
    r"bundle missing|missing (lib|bundle|gem|runtime|dependenc|interpreter|binary|psych|libyaml)|"
    r"libyaml|psych|"
    r"(is|are|was|were)? ?(not|un)\s?available|"
    r"in (this|the) (environment|sandbox)|in the sandbox",
    re.IGNORECASE,
)
def _is_bug_like_ticket(score: Score) -> bool:
    """Heuristic: detect bug-fix tickets for step-label wording."""
    text = f"{score.title}\n{score.description}".lower()
    bug_keywords = ("bug", "fix", "regression", "broken", "error", "defect", "crash", "issue")
    return any(word in text for word in bug_keywords)
_VISUAL_TASK_PATTERN = re.compile(
    r"\b(?:"
    + "|".join(re.escape(keyword) for keyword in _VISUAL_TASK_KEYWORDS)
    + r")\b",
    re.IGNORECASE,
)


def _looks_like_url(*a, **k):
    """Lazy delegate to performer.main (import cycle + test patch targets)."""
    from performer import main as _m

    return _m._looks_like_url(*a, **k)


# --- QA-only helpers, moved verbatim from main.py -------------------------------

def _normalise_steps(value: object) -> list[str]:
    """Normalize backend-provided step lists into non-empty strings."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            step = item.strip()
            if step:
                out.append(step)
            continue
        if isinstance(item, dict):
            step = str(item.get("step", item.get("text", ""))).strip()
            if step:
                out.append(step)
    # Preserve order while removing duplicates.
    deduped: list[str] = []
    seen: set[str] = set()
    for step in out:
        if step in seen:
            continue
        seen.add(step)
        deduped.append(step)
    return deduped

def _normalise_visual_evidence(value: object) -> list[dict[str, str]]:
    """Normalize visual evidence items into a consistent dict shape."""
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            loc = item.strip()
            if loc:
                out.append({
                    "label": "Evidence",
                    "kind": "artifact",
                    "path_or_url": loc,
                    "note": "",
                })
            continue
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "Evidence")).strip() or "Evidence"
        kind = str(item.get("kind", "artifact")).strip() or "artifact"
        loc = str(item.get("path_or_url", item.get("url", item.get("path", "")))).strip()
        note = str(item.get("note", item.get("description", ""))).strip()
        if not loc and not note:
            continue
        out.append({
            "label": label,
            "kind": kind,
            "path_or_url": loc,
            "note": note,
        })
    return out[:20]

def _has_local_visual_artifact(visual_evidence: list[dict[str, str]]) -> bool:
    """True if any evidence entry points at a local file that actually exists.

    Used to decide whether the deterministic capture backstop is needed: an
    already-uploaded URL, or a local path the QA agent genuinely produced, means
    we don't re-capture. A claimed-but-absent local path does NOT count.
    """
    for ev in visual_evidence:
        loc = str(ev.get("path_or_url", "")).strip()
        if not loc:
            continue
        if _looks_like_url(loc):
            return True
        try:
            if Path(loc).is_file() and Path(loc).stat().st_size > 0:
                return True
        except OSError:
            continue
    return False

def _qa_visual_validation_required(
    *,
    score: Score,
    qa_output: dict,
    demo_setup_steps: list[str],
    visual_capture_blockers: list[str],
    visual_evidence: list[dict[str, str]],
) -> bool:
    """Determine whether this QA run must include visual artifacts."""
    raw = qa_output.get("visual_validation_required")
    if isinstance(raw, bool):
        return raw

    if demo_setup_steps or visual_capture_blockers or visual_evidence:
        return True

    content = "\n".join(
        [
            score.title,
            score.description,
            "\n".join(score.acceptance_criteria or []),
        ],
    )
    return _VISUAL_TASK_PATTERN.search(content) is not None


def _delete_qa_capture_dir(qa_output: dict) -> None:
    """Delete the workflow's qa-visual-* evidence tree, once and safely.

    Managed lifecycle (411 round-two review): finalize_qa is the last reader
    of the container-local capture paths, so it removes the tree instead of
    leaving one per run in /tmp. Round-four review added two constraints:

    - The path arrives from unvalidated model JSON. Only a directory the
      workflow could have created qualifies: named qa-visual-* (mkdtemp's
      prefix) and resolving strictly inside the system temp dir. Anything
      else — a workspace path, /tmp itself, a symlinked escape — is left
      untouched and never touched.
    - Every exit path must call this: the test-commit failure return returns
      before resolve runs, and its leak would never see the main cleanup.
    """
    raw = str(qa_output.get("visual_capture_dir") or "")
    if not raw:
        return
    candidate = Path(raw)
    if not candidate.name.startswith("qa-visual-"):
        return
    tmp_root = Path(tempfile.gettempdir()).resolve()
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        # A symlink loop (RuntimeError) or an unreadable path (OSError)
        # cannot be validated as workflow-owned; leave it untouched rather
        # than raising through the whole QA response (411 round-eight
        # review).
        return
    if not resolved.is_relative_to(tmp_root):
        return
    shutil.rmtree(candidate, ignore_errors=True)

def _qa_visual_evidence_failures(
    *,
    required: bool,
    stand_path: Path,
    visual_capture_commands: list[str],
    visual_capture_blockers: list[str],
    visual_evidence: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Return synthetic QA failures for missing/invalid visual evidence."""
    if not required:
        return []

    failures: list[dict[str, str]] = []
    if not visual_evidence:
        attempts_text = " | ".join(visual_capture_commands[:3]) if visual_capture_commands else "none"
        blockers_text = " | ".join(visual_capture_blockers[:3]) if visual_capture_blockers else "none"
        failures.append(
            {
                "criterion": "Visual evidence artifacts captured",
                "expected": "At least one screenshot/GIF/video/artifact generated from this QA run.",
                "actual": (
                    f"No artifacts captured. Commands attempted: {attempts_text}. "
                    f"Blockers: {blockers_text}."
                ),
                "test": "visual-capture",
            },
        )
        return failures

    has_artifact_location = False
    missing_location_count = 0
    for ev in visual_evidence:
        loc = str(ev.get("path_or_url", "")).strip()
        if not loc:
            missing_location_count += 1
            continue
        has_artifact_location = True
        if _looks_like_url(loc):
            continue
        candidate = Path(loc)
        artifact_path = candidate if candidate.is_absolute() else (stand_path / candidate)
        if artifact_path.exists():
            continue
        failures.append(
            {
                "criterion": "Visual evidence artifact path exists",
                "expected": "Each local visual artifact path points to an existing file.",
                "actual": f"Artifact path does not exist: {loc}",
                "test": "visual-capture",
            },
        )
    if missing_location_count:
        failures.append(
            {
                "criterion": "Visual evidence entries include artifact locations",
                "expected": "Each visual evidence entry includes a non-empty path_or_url.",
                "actual": (
                    f"{missing_location_count} visual evidence entr"
                    f"{'y is' if missing_location_count == 1 else 'ies are'} missing path_or_url."
                ),
                "test": "visual-capture",
            },
        )
    if not has_artifact_location:
        failures.append(
            {
                "criterion": "Visual evidence artifacts captured",
                "expected": "At least one visual evidence entry includes a screenshot/GIF/video path_or_url.",
                "actual": "Only note-only visual evidence entries were provided.",
                "test": "visual-capture",
            },
        )
    return failures

def _default_verification_steps(score: Score) -> list[str]:
    """Generate fallback verification steps when backend omitted them."""
    criteria = [str(c).strip() for c in (score.acceptance_criteria or []) if str(c).strip()]
    if criteria:
        return [f"Verify acceptance criterion: {criterion}" for criterion in criteria[:10]]
    return [
        "Run the relevant automated tests and confirm they pass.",
        "Manually validate the changed user flow end-to-end and confirm expected behavior.",
    ]

def _env_blocked_qa_response(perf: Any, env_error: str) -> "PerformerResponse":
    """The qa_env_blocked verdict, in one place.

    Review of #115: the recorded services-start failure is consumed on two paths
    -- here, when the backend's output cannot be parsed at all, and in the
    verdict path, where it is folded into ``environment_error``. Both were
    correct and each built its own report shape, so the next field added to a QA
    report had to be added twice with nothing failing if only one was done.

    The zeroes are not arbitrary: ``qa_verdict.py`` reads ``criteria_checked``
    for its zero-evidence floor, so this shape is what makes coordinare treat the
    verdict as "could not verify" rather than "verified nothing". Do not tidy a
    field out of it without reading that gate.
    """
    perf.qa_report = {
        "criteria_checked": 0,
        "criteria_passed": 0,
        "evidence_count": 0,
        "env_limited": True,
        "environment_error": env_error,
    }
    perf.state = "qa_env_blocked"
    return PerformerResponse(
        status="qa_env_blocked",
        session_id=perf.session_id,
        reason=env_error,
        report=perf.qa_report,
    )

def _qa_safe_int(value: object) -> int:
    """Coerce an unvalidated JSON count safely, refusing instead of raising.

    Counts ride unvalidated model JSON, and counts are counts: a non-negative
    integer or nothing. A "unknown" string is not a number at all, NaN/Infinity
    are floats int() rejects, and a bool is not a count just because it
    subclasses int — nor are negatives or fractional values a checked-criteria
    count. Every malformed value coerces to 0: a refused pass, never a crashed
    response and never a bypass of the zero-criteria gate (411 round-seven
    review).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def _qa_failure_is_environmental(f: dict) -> bool:
    """077: True if a QA failure reflects the container's inability to VERIFY
    (no DB/browser/binary on PATH, read-only fs, capture blocked) rather than a
    real code defect. Environmental limits are advisory — they must not block the
    lifecycle; only genuine defects do."""
    ftype = str(f.get("type", "")).lower()
    if ftype in {"visual-capture", "environment", "env"}:
        return True
    # 054: the branch-freshness gate is a deliberate hard block — an indeterminate
    # freshness state ("could not be verified") must NOT pass, even though its
    # phrasing looks environmental. Exclude it before the regex so the broadened
    # "couldn't check" patterns can't accidentally wave a stale branch through.
    if ftype == "freshness_indeterminate":
        return False
    # 411 (AC8): criterion/expected quote the CARD — what to verify — not what
    # went wrong. A criterion may legitimately mention infrastructure
    # ("database is down") while the failure is a code defect; classifying on
    # the quote inverted the verdict. Only what the run OBSERVED classifies.
    # Workflow Finding dicts store their run evidence in `observed` (the
    # adapter forwards the model dump), so it must join the blob or an
    # environment-only workflow finding reads as a hard defect (round-eight).
    blob = " ".join(
        str(f.get(k, "")) for k in ("message", "actual", "observed", "test")
    )
    return bool(_QA_ENV_FAILURE_PATTERNS.search(blob))

def _qa_execution_evidence(qa_output: dict) -> list[dict]:
    """083: normalise the model's self-reported executed checks. Each entry must
    carry a non-empty command AND a recorded exit_code; entries lacking either
    are dropped so a model can't manufacture evidence with empty objects.

    Returns the cleaned list (possibly empty). This is the primary signal the
    unsubstantiated-pass guard uses to tell a real verification from a
    rubber-stamp."""
    raw = qa_output.get("executed_checks", [])
    if not isinstance(raw, list):
        return []
    cleaned: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        command = str(entry.get("command", "")).strip()
        exit_code = entry.get("exit_code")
        if command and exit_code is not None:
            cleaned.append(
                {
                    "command": command,
                    "exit_code": exit_code,
                    "output": str(entry.get("output", "")).strip(),
                },
            )
    return cleaned

def _qa_unsubstantiated_pass(
    *,
    qa_passed_flag: bool,
    criteria_passed: object,
    executed_checks: list[dict],
    new_tests: list[str],
    visual_evidence: list[dict],
) -> bool:
    """083: a claimed QA pass must rest on OBSERVED evidence, not self-report.

    gpt-oss:120b was caught emitting ``criteria_passed=N/N`` with ``failures: []``
    while never running a single check — a rubber-stamp that slipped a known bug
    through the gate. local/qwen3-14b then exploited the mirror loophole: it
    claimed ``qa_passed`` with ``criteria_passed=0`` and an empty ``executed_checks``
    (verifying nothing at all), which the original ``criteria_passed<=0`` exemption
    let through. Both are the same failure — a confident pass that rests on no
    OBSERVED evidence — so refuse a pass whenever there is no execution evidence
    (no executed_checks, no committed tests, no captured proof), regardless of
    the self-reported criteria count.

    088 (FR-001): the env_limited exemption is gone. Website PR #159 exploited
    it — "couldn't verify" plus a claimed 4/4 pass posted an unflagged clean
    PASS. A zero-evidence pass claim is unsubstantiated regardless of
    environment_error; the CALLER decides the classification (env-limited →
    ``qa_env_blocked``, otherwise the malformed/unsubstantiated refusal).

    ``criteria_passed`` is accepted but no longer gates the check: a pass that
    asserted nothing positive yet still claimed success is the purest rubber-stamp,
    not an exemption."""
    del criteria_passed  # retained for signature/observability; no longer gates
    if not qa_passed_flag:
        return False  # already failing on a real defect
    has_evidence = (
        bool(executed_checks)
        or bool(new_tests)
        or any(ev.get("path_or_url") for ev in visual_evidence)
    )
    return not has_evidence

def _qa_env_limited_without_verification(
    *,
    qa_passed_flag: bool,
    env_limited: bool,
    criteria_checked: object,
    criteria_passed: object,
) -> bool:
    """120 (US1): True when an env-limited advisory pass verified ZERO criteria.

    The env-limited advisory pass (077) lets a run whose only failures are
    environmental pass DEGRADED. But a run that passed *zero* criteria
    (``criteria_passed == 0`` while criteria were checked) verified nothing — it
    is "couldn't verify", not a pass — even when the failed attempts left some
    evidence (so ``_qa_unsubstantiated_pass``, which only guards the
    zero-evidence case, would let it through). The caller routes such a run to
    ``qa_env_blocked`` (HOLD for cache repair) instead of the advisory pass. The
    advisory pass remains valid only when at least one criterion genuinely
    passed. Non-numeric counts coerce to 0 (never raises)."""
    if not (qa_passed_flag and env_limited):
        return False
    try:
        checked = int(criteria_checked or 0)
    except (TypeError, ValueError):
        checked = 0
    try:
        passed = int(criteria_passed or 0)
    except (TypeError, ValueError):
        passed = 0
    return checked > 0 and passed == 0

def _qa_app_boot_evidence(
    qa_output: dict,
    executed_checks: list[dict],
) -> tuple[dict | None, bool]:
    """088 (FR-005): parse the model-reported ``app_boot_check`` and decide
    whether it constitutes boot proof for visual/UI criteria.

    Boot proof requires a non-empty command with exit_code 0 that references an
    entry in ``executed_checks`` — the model must have actually RUN the health
    check, not merely asserted it. Returns ``(parsed, ok)``; ``parsed`` is
    ``{"command", "exit_code"}`` or None when the field is absent/malformed."""
    raw = qa_output.get("app_boot_check")
    if not isinstance(raw, dict):
        return None, False
    command = str(raw.get("command", "")).strip()
    exit_code = raw.get("exit_code")
    if not command or exit_code is None:
        return None, False
    parsed = {"command": command, "exit_code": exit_code}
    referenced = any(check.get("command") == command for check in executed_checks)
    return parsed, bool(referenced and exit_code == 0)

def _build_qa_pr_comment(
    *,
    score: Score,
    passed: bool,
    criteria_checked: int,
    criteria_passed: int,
    failures: list[dict],
    verification_steps: list[str],
    pre_fix_repro_steps: list[str],
    demo_setup_steps: list[str],
    visual_capture_commands: list[str],
    visual_capture_blockers: list[str],
    visual_evidence: list[dict[str, str]],
    environment_error: str,
    evidence_count: int | None = None,
    env_blocked: bool = False,
) -> str:
    """Render a human-facing QA evidence comment for the PR thread."""
    bug_like = _is_bug_like_ticket(score)
    verify_heading = "Fix Verification Steps" if bug_like else "Demo / Verification Steps"
    if env_blocked:
        result_label = "ENVIRONMENT-BLOCKED — UNVERIFIED"
    else:
        result_label = "PASSED" if passed else "FAILED"
    # 088 (FR-007): cross-validate the claimed count against the evidence the
    # run actually produced; surplus claims are annotated, not hidden.
    criteria_passed_line = f"- Criteria passed: {criteria_passed}"
    if evidence_count is not None and criteria_passed > evidence_count:
        criteria_passed_line = (
            f"- Criteria passed: {criteria_passed} claimed / "
            f"{evidence_count} evidence-backed"
        )
    lines = [
        _persona_tag(score),
        "",
        "## QA Evidence",
        "",
        f"**Result:** {result_label}",
    ]

    # 088 (FR-002): an environment-blocked verdict leads with the blocker.
    if env_blocked and environment_error:
        lines += [
            "",
            "### Environment Blocker",
            f"- {environment_error}",
            "",
        ]

    lines += [
        f"- Criteria checked: {criteria_checked}",
        criteria_passed_line,
    ]

    if environment_error and not env_blocked:
        lines += [
            "",
            "### Environment Blocker",
            f"- {environment_error}",
        ]

    if pre_fix_repro_steps:
        lines += ["", "### Known Reproduction Steps (pre-fix context)"]
        lines.extend(f"{idx}. {step}" for idx, step in enumerate(pre_fix_repro_steps, start=1))

    if demo_setup_steps:
        lines += ["", "### Visual Capture Setup Steps"]
        lines.extend(f"{idx}. {step}" for idx, step in enumerate(demo_setup_steps, start=1))

    if visual_capture_commands:
        lines += ["", "### Visual Capture Commands Attempted"]
        lines.extend(f"{idx}. `{cmd}`" for idx, cmd in enumerate(visual_capture_commands, start=1))

    lines += ["", f"### {verify_heading}"]
    lines.extend(f"{idx}. {step}" for idx, step in enumerate(verification_steps, start=1))

    # 088 (FR-006): only upload-validated entries (CDN/http URLs after
    # resolve_visual_evidence_urls) render as links; entries still carrying a
    # local path failed to publish and are listed as capture blockers with the
    # reason — a dead link implies an artifact that does not exist.
    published: list[dict[str, str]] = []
    capture_blockers = list(visual_capture_blockers)
    for ev in visual_evidence:
        loc = str(ev.get("path_or_url", "")).strip()
        if not loc:
            continue
        if _looks_like_url(loc):
            published.append(ev)
        else:
            reason = str(ev.get("upload_error", "")).strip() or (
                "artifact not published (upload failed or container-local path)"
            )
            capture_blockers.append(
                f"`{loc}` ({ev.get('label', 'Evidence')}) — {reason}",
            )

    lines += ["", "### Visual Evidence"]
    if published:
        for ev in published:
            label = ev.get("label", "Evidence")
            kind = ev.get("kind", "artifact")
            loc = ev.get("path_or_url", "")
            note = ev.get("note", "")
            is_image = kind == "screenshot" or Path(loc).suffix.lower() in {
                ".png", ".jpg", ".jpeg", ".gif", ".webp",
            }
            line = f"- **{label}** ({kind})"
            if is_image:
                line += f": [{loc}]({loc})\n\n  ![{label}]({loc})"
            else:
                line += f": [{loc}]({loc})"
            if note:
                line += f" — {note}"
            lines.append(line)
    else:
        lines.append("- No visual artifacts captured in this QA run.")
    if capture_blockers:
        lines.append("- Capture blockers:")
        lines.extend(f"  - {blocker}" for blocker in capture_blockers)

    if failures:
        lines += ["", "### Remaining Failures"]
        for failure in failures[:10]:
            criterion = str(failure.get("criterion", ""))
            expected = str(failure.get("expected", ""))
            actual = str(failure.get("actual", ""))
            lines.append(f"- **{criterion or 'Unnamed criterion'}** — expected `{expected}`, got `{actual}`")

    return "\n".join(lines)
