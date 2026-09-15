"""411 — QA verdict integrity, performer-side half.

The workflow now computes an honest verdict and emits it as `passed` in the
report. These pin that the post-processing honours that verdict instead of
recomputing "pass" from the failures list alone — which cannot see
regressions, env-blocked runs, or zero-criteria passes.

Mirrors the TestQAPerformer scaffold in test_main.py; the behavioural net
there (44 role="qa" handle_status tests) keeps the untouched behaviour.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from performer.backends.base import BackendStatus
from performer.config import Settings
from performer.main import handle_status
from performer.models import Performance, Score, Stand
from performer.protocol import PerformerMessage


def _msg(action: str, session_id: str = "", **payload) -> PerformerMessage:  # type: ignore[type-arg]
    return PerformerMessage(action=action, session_id=session_id, payload=payload)  # type: ignore[arg-type]


def _qa_output(**over) -> str:
    """The workflow-report shape the adapter now produces (report + findings)."""
    base = {
        "passed": True,
        "failures": [],
        "criteria_checked": 1,
        "criteria_passed": 1,
        "executed_checks": [
            {"command": "pytest -q", "exit_code": 0, "output": "1 passed"},
        ],
    }
    base.update(over)
    return json.dumps(base)


class TestWorkflowVerdictIntegration:
    """AC1/AC7: the workflow's own verdict is the verdict."""

    def _make_perf(self) -> Performance:
        stand = Stand(path=Path("/tmp/fake"), branch="feat/test")
        stand.git_env = {}
        score = Score(title="Test", repo_url="https://github.com/acme/repo", branch="feat/test")
        return Performance(session_id="sid", stand=stand, score=score, backend=MagicMock(), role="qa")

    async def _handle(self, output: str, perf: Performance):
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with patch("performer.main.commit_file", new=AsyncMock()):
            return await handle_status(
                _msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode")
            )

    @pytest.mark.asyncio
    async def test_a_workflow_reported_failure_bounces_even_without_unmet_criteria(self) -> None:
        """The regression that motivated 411: the post-processing recomputed
        the pass from the failures list, which only carries unmet_criterion
        entries. A run whose workflow verdict is failed for any OTHER reason
        (silent regression, judge refusal) had `passed: false` thrown away and
        reported qa_passed."""
        perf = self._make_perf()
        resp = await self._handle(_qa_output(
            passed=False,
            qa_findings=[{
                "category": "unexpected_regression",
                "severity": "high",
                "expected": "a password field on /signin",
                "observed": "the password field is gone",
            }],
        ), perf)

        assert resp.status != "qa_passed", (
            "the workflow said failed; the post-processing must not overrule it"
        )
        assert resp.status == "qa_failed"
        assert any("workflow" in f.get("criterion", "").lower() for f in resp.failures), (
            "the bounce must say WHY: the workflow's own verdict"
        )

    @pytest.mark.asyncio
    async def test_a_workflow_regression_is_not_masked_by_a_capture_failure(self) -> None:
        """411 round-five review: the synthetic visual-capture failure makes
        env_limited true, but the workflow's OWN findings carry a hard defect.
        The defect wins: checked-and-broken is qa_failed, never
        qa_env_blocked."""
        perf = self._make_perf()
        resp = await self._handle(_qa_output(
            passed=False,
            failures=[],  # the report's failures payload: only unmet criteria ride here
            qa_findings=[{
                "category": "unexpected_regression",
                "severity": "high",
                "expected": "a password field on /signin",
                "observed": "the password field is gone",
            }],
            visual_validation_required=True, visual_evidence=[],
        ), perf)

        assert resp.status == "qa_failed", (
            "a real regression is a defect even when capture also failed"
        )

    @pytest.mark.asyncio
    async def test_a_missing_baseline_run_routes_to_env_blocked(self) -> None:
        """A run that could not build its baseline reports passed=false with an
        environment_error and NON-empty evidence (the baseline runs before the
        checks, so executed_checks is not empty). It slipped past every gate
        into an advisory pass. It is 'couldn't verify'."""
        perf = self._make_perf()
        resp = await self._handle(_qa_output(
            passed=False,
            environment_error="baseline unavailable: RuntimeError: the base app never came up",
        ), perf)

        assert resp.status == "qa_env_blocked", (
            "a missing baseline is a hold, never a pass and never a bounce"
        )

    @pytest.mark.asyncio
    async def test_a_zero_criteria_workflow_pass_is_refused(self) -> None:
        """criteria_checked=0 with evidence present satisfies the
        unsubstantiated gate's evidence floor, so a vacuous pass — the judge
        never reconciled a single criterion — sailed through. Zero checked
        criteria is not a pass; it is a run that verified nothing."""
        perf = self._make_perf()
        resp = await self._handle(_qa_output(
            passed=True, criteria_checked=0, criteria_passed=0,
        ), perf)

        assert resp.status != "qa_passed"
        assert resp.status == "qa_failed"
        assert any("no acceptance criteria" in f.get("criterion", "").lower()
                   or "zero acceptance criteria" in f.get("criterion", "").lower()
                   for f in resp.failures)

    @pytest.mark.asyncio
    async def test_legacy_outputs_without_a_verdict_key_are_untouched(self) -> None:
        """Backends predating the workflow report no `passed` key. The new
        gates must not change their verdicts."""
        perf = self._make_perf()
        resp = await self._handle(json.dumps({
            "failures": [],
            "criteria_checked": 5,
            "criteria_passed": 5,
            "executed_checks": [
                {"command": "pytest -q", "exit_code": 0, "output": "5 passed"},
            ],
        }), perf)

        assert resp.status == "qa_passed"

    @pytest.mark.asyncio
    async def test_a_genuine_workflow_pass_still_passes(self) -> None:
        """passed=true with evidence and no findings is the good path."""
        perf = self._make_perf()
        resp = await self._handle(_qa_output(), perf)
        assert resp.status == "qa_passed"

    @pytest.mark.asyncio
    async def test_the_fallback_capture_boots_the_shape_reading(self, monkeypatch) -> None:
        """411 review: the workflow's shape reading rides in the report as
        app_start_command, so the fallback capture boots the same command the
        workflow would have — not a fresh guess from the framework heuristics."""
        seen: dict = {}

        def fake_capture(**kwargs):
            seen.update(kwargs)
            return None

        monkeypatch.setattr("performer.main.boot_and_capture_app_screenshot", fake_capture)
        perf = self._make_perf()
        await self._handle(_qa_output(
            passed=True, criteria_checked=1, criteria_passed=1,
            visual_validation_required=True, visual_evidence=[],
            app_start_command="bin/rails server -b 0.0.0.0",
        ), perf)

        shape = seen.get("shape")
        assert shape is not None and shape.start_command == "bin/rails server -b 0.0.0.0", (
            "the shape reading must reach the fallback capture"
        )

    @pytest.mark.asyncio
    async def test_workflow_env_overrides_reach_the_fallback_capture(self, monkeypatch) -> None:
        """411 review: the capture env layers identically to the workflow's
        boot_env — os env, then the env cache, then the operator's
        workflow_env last and highest."""
        seen: dict = {}

        def fake_capture(**kwargs):
            seen.update(kwargs)
            return None

        monkeypatch.setattr("performer.main.boot_and_capture_app_screenshot", fake_capture)
        perf = self._make_perf()
        perf.score.workflow_env = {"QA_APP_START_COMMAND": "bin/rails server", "PORT": "9999"}
        await self._handle(_qa_output(
            passed=True, criteria_checked=1, criteria_passed=1,
            visual_validation_required=True, visual_evidence=[],
        ), perf)

        env = seen.get("env") or {}
        assert env["PORT"] == "9999", "the operator's workflow_env wins over the process env"
        assert env["QA_APP_START_COMMAND"] == "bin/rails server"

    @pytest.mark.asyncio
    async def test_a_malformed_criteria_count_cannot_crash_the_response(self, monkeypatch) -> None:
        """411 round-three review: criteria_checked arrives from unvalidated
        model JSON. The zero-criteria gate coerced it with a bare int(), so a
        value like "unknown" raised and took the whole QA response with it."""
        monkeypatch.setattr(
            "performer.main.boot_and_capture_app_screenshot", lambda **_kw: None
        )
        perf = self._make_perf()
        resp = await self._handle(_qa_output(
            passed=True, criteria_checked="unknown", criteria_passed="also unknown",
        ), perf)

        assert resp.status != "qa_passed", (
            "a malformed count is not a verified pass"
        )

    @pytest.mark.asyncio
    async def test_a_non_finite_criteria_count_cannot_crash_the_response(self, monkeypatch) -> None:
        """411 round-six review: NaN/Infinity are valid JSON numbers that
        int() also rejects — a reported pass with criteria_checked: NaN raised
        ValueError and prevented any QA response. They coerce to zero and the
        pass is refused instead."""
        monkeypatch.setattr(
            "performer.main.boot_and_capture_app_screenshot", lambda **_kw: None
        )
        perf = self._make_perf()
        resp = await self._handle(_qa_output(
            passed=True, criteria_checked=float("nan"), criteria_passed=0,
        ), perf)

        assert resp.status != "qa_passed", (
            "a non-finite count is not a verified pass"
        )

    @pytest.mark.asyncio
    async def test_the_evidence_tree_is_deleted_after_upload(self, tmp_path) -> None:
        """411 review: the workflow's qa-visual-* tree is the consumer's to
        clean. finalize_qa is the last reader of those container-local paths,
        so the directory must not outlive it."""
        evidence = tmp_path / "qa-visual-xyz"
        evidence.mkdir()
        shot = evidence / "qa_visual_c1.png"
        shot.write_bytes(b"png")

        perf = self._make_perf()
        await self._handle(_qa_output(
            passed=True, criteria_checked=1, criteria_passed=1,
            visual_validation_required=True, visual_evidence=[
                {"label": "shot", "kind": "screenshot", "path_or_url": str(shot)},
            ],
            visual_capture_dir=str(evidence),
        ), perf)

        assert not evidence.exists(), "the evidence tree is deleted after post-processing"

    @pytest.mark.asyncio
    async def test_the_evidence_urls_resolve_before_the_report_is_committed(self) -> None:
        """411 round-seven review: the committed qa.md was built before the
        CDN upload ran, so it recorded container-local paths the cleanup then
        deleted, and only the PR comment got the published URLs. Resolve
        must run first."""
        from performer import main as main_mod

        order: list[str] = []

        async def fake_resolve(visual_evidence, **_kw):
            order.append("resolve")
            return visual_evidence

        async def fake_commit(stand, path, content, message):
            order.append("commit")

        perf = self._make_perf()
        perf.backend.get_status.return_value = BackendStatus(
            state="done", output=_qa_output(
                passed=True, criteria_checked=1, criteria_passed=1,
                visual_evidence=[{"label": "s", "kind": "screenshot", "path_or_url": "/tmp/x.png"}],
            )
        )
        with patch("performer.main.resolve_visual_evidence_urls", fake_resolve), \
                patch("performer.main.commit_file", fake_commit):
            await handle_status(
                _msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode")
            )

        assert order.index("resolve") < order.index("commit"), (
            "the committed report records resolved URLs, not local paths"
        )

    @pytest.mark.asyncio
    async def test_a_malformed_qa_findings_list_cannot_crash_the_response(self) -> None:
        """411 round-seven review: qa_findings comes from unvalidated JSON; a
        string member must be filtered, not crash on .get()."""
        perf = self._make_perf()
        resp = await self._handle(_qa_output(
            passed=False, failures=[], qa_findings=["not a dict"],
        ), perf)

        assert resp.status == "qa_failed", (
            "a malformed findings list still produces a failed verdict"
        )

    @pytest.mark.asyncio
    async def test_the_failure_payload_carries_the_workflow_s_own_findings(self) -> None:
        """411 round-seven review: a passed:false verdict with detailed
        findings gave the implementer only a generic workflow_verdict refusal.
        The actionable detail — expected, observed — rides in the payload."""
        perf = self._make_perf()
        await self._handle(_qa_output(
            passed=False, failures=[],
            qa_findings=[{
                "category": "unexpected_regression",
                "severity": "high",
                "expected": "a password field on /signin",
                "observed": "the password field is gone",
            }],
        ), perf)

        failures = perf.qa_failures
        assert any(f.get("type") == "unexpected_regression" for f in failures), (
            "the workflow's own finding is surfaced"
        )
        assert any(f.get("actual") == "the password field is gone" for f in failures), (
            "the observed detail reaches the repair payload"
        )

    @pytest.mark.asyncio
    async def test_an_environmental_observed_finding_routes_to_env_blocked(self) -> None:
        """411 round-eight review: workflow Finding dicts store run evidence
        in `observed`, but the classifier read only `actual` — an
        environment-only finding classified as a hard defect and the run
        reported qa_failed instead of qa_env_blocked."""
        perf = self._make_perf()
        resp = await self._handle(_qa_output(
            passed=False, failures=[],
            qa_findings=[{
                "category": "step_unavailable",
                "severity": "high",
                "expected": "the users endpoint responds",
                "observed": "connection refused",
            }],
            visual_validation_required=True, visual_evidence=[],
        ), perf)

        assert resp.status == "qa_env_blocked", (
            "observed evidence must classify; an env-only finding blocks, not defects"
        )

    @pytest.mark.asyncio
    async def test_workflow_findings_merge_even_when_the_payload_already_has_a_defect(self) -> None:
        """411 round-eight review: the workflow-verdict block was gated on
        qa_passed_flag, so an unmet criterion in the failures payload
        suppressed every additional workflow finding — the implementer lost
        the regression detail while the verdict stayed failed."""
        perf = self._make_perf()
        await self._handle(_qa_output(
            passed=False,
            failures=[{
                "type": "unmet_criterion",
                "criterion": "signing in with a workspace lands on the workspace",
                "expected": "the workspace page",
                "actual": "an empty redirect",
            }],
            qa_findings=[{
                "category": "unexpected_regression",
                "severity": "high",
                "expected": "a password field on /signin",
                "observed": "the password field is gone",
            }],
        ), perf)

        types = [f.get("type") for f in perf.qa_failures]
        assert "unmet_criterion" in types, "the payload's own defect survives"
        assert "unexpected_regression" in types, "the workflow finding is merged too"

    @pytest.mark.asyncio
    async def test_a_bool_or_negative_criteria_count_cannot_bypass_the_zero_criteria_gate(self, monkeypatch) -> None:
        """411 round-seven review: bool subclasses int, and negatives or
        fractional floats passed the coercion — criteria_checked: true with
        real evidence bypassed the zero-criteria refusal. Counts are
        non-negative integers or zero."""
        monkeypatch.setattr(
            "performer.main.boot_and_capture_app_screenshot", lambda **_kw: None
        )
        for bad in (True, -1, 1.5):
            perf = self._make_perf()
            resp = await self._handle(_qa_output(
                passed=True, criteria_checked=bad, criteria_passed=bad,
            ), perf)

            assert resp.status != "qa_passed", f"{bad!r} is not a verified pass"

    @pytest.mark.asyncio
    async def test_an_untrusted_visual_capture_dir_is_never_removed(self, tmp_path) -> None:
        """411 round-four review: visual_capture_dir arrives from unvalidated
        model JSON. Only a workflow-shaped qa-visual-* tree under the system
        temp dir may be removed; anything else is left untouched."""
        decoy = tmp_path / "not-qa-visual"
        decoy.mkdir()

        perf = self._make_perf()
        await self._handle(_qa_output(
            passed=True, criteria_checked=1, criteria_passed=1,
            visual_validation_required=True, visual_evidence=[],
            visual_capture_dir=str(decoy),
        ), perf)

        assert decoy.exists(), "an untrusted path is never the cleanup's target"

    @pytest.mark.asyncio
    async def test_the_evidence_tree_is_cleaned_on_the_test_commit_exit(self, tmp_path) -> None:
        """411 round-four review: the new_test_files commit failure returns
        before resolve runs, so its exit must make the same cleanup promise
        instead of leaking one qa-visual-* tree per attempt."""
        evidence = tmp_path / "qa-visual-abc"
        evidence.mkdir()

        perf = self._make_perf()
        perf.backend.get_status.return_value = BackendStatus(state="done", output=_qa_output(
            passed=True, criteria_checked=1, criteria_passed=1,
            new_test_files=[{"path": "tests/x.py", "content": "def t(): pass"}],
            visual_capture_dir=str(evidence),
        ))
        with patch(
            "performer.main.commit_file",
            new=AsyncMock(side_effect=RuntimeError("git broke")),
        ):
            resp = await handle_status(
                _msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode")
            )

        assert resp.status == "error", "the commit failure is an error exit"
        assert not evidence.exists(), "the early exit still cleans the evidence tree"


class TestEnvironmentalPatternScope:
    """AC8: env patterns only match evidence that is not the criterion text."""

    def test_a_failure_is_not_environmental_because_the_criterion_mentions_the_word(self) -> None:
        from performer.qa_postprocess import _qa_failure_is_environmental

        failure = {
            "message": "the sign-in page kept the button disabled",
            "criterion": "Users can sign in even while the database is down",
            "expected": "the workspace dropdown is visible after sign-in",
            "actual": "button stayed disabled after clicking",
            "test": "test_sign_in.py::test_disabled_button",
        }
        assert not _qa_failure_is_environmental(failure), (
            "the criterion's own wording is the thing under test; matching it "
            "turns every defect on an environment-flavoured criterion into an "
            "advisory pass"
        )

    def test_a_failure_whose_own_text_reports_the_environment_stays_advisory(self) -> None:
        from performer.qa_postprocess import _qa_failure_is_environmental

        failure = {
            "message": "pytest could not connect to postgres at 127.0.0.1:5432",
            "actual": "psycopg2.OperationalError",
        }
        assert _qa_failure_is_environmental(failure)


class TestFallbackCaptureStartCommand:
    """AC5: the fallback capture boots the shape's start command, and the
    operator override wins — with VAR=val assignments riding in the spawn env."""

    @staticmethod
    def _fake_proc():
        class P:
            def terminate(self): pass
            def kill(self): pass
            def wait(self, timeout=None): return 0
            def poll(self): return None

        return P()

    def test_the_shape_start_command_feeds_the_fallback_boot(self, tmp_path) -> None:
        from performer.workflows.project_shape import ProjectShape
        from performer.qa_capture import resolve_start_command

        resolved = resolve_start_command(
            tmp_path, {"PORT": "8000"},
            ProjectShape(start_command="bin/serve --port 9000"),
        )
        assert resolved == (["bin/serve", "--port", "9000"], {})

    def test_the_operator_override_beats_the_shape(self, tmp_path) -> None:
        from performer.workflows.project_shape import ProjectShape
        from performer.qa_capture import resolve_start_command

        resolved = resolve_start_command(
            tmp_path,
            {"PORT": "8000", "QA_APP_START_COMMAND": "python app.py"},
            ProjectShape(start_command="bin/serve"),
        )
        assert resolved == (["python", "app.py"], {})

    def test_an_env_assignment_prefix_rides_in_the_spawn_env(self, tmp_path) -> None:
        """The spawner takes an argv list; `RAILS_ENV=test bin/rails server`
        split naively runs 'RAILS_ENV=test' as argv[0]. The assignment is a
        child process env var, not a program name."""
        from performer.workflows.project_shape import ProjectShape
        from performer.qa_capture import resolve_start_command

        resolved = resolve_start_command(
            tmp_path,
            {"PORT": "8000", "QA_APP_START_COMMAND": "RAILS_ENV=test bin/rails server -b 0.0.0.0"},
            ProjectShape(),
        )
        assert resolved == (["bin/rails", "server", "-b", "0.0.0.0"], {"RAILS_ENV": "test"})

    def test_a_commandless_shape_still_falls_back_to_inference(self, tmp_path) -> None:
        """The Rails/Django/Node inference survives: the shape layer is in
        front of it, not instead of it."""
        from performer.workflows.project_shape import ProjectShape
        from performer.qa_capture import resolve_start_command

        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "rails").write_text("#!/bin/sh\n")
        resolved = resolve_start_command(
            tmp_path, {"PORT": "8000", "RAILS_ENV": "test"}, ProjectShape(start_command=""),
        )
        assert resolved is not None
        argv, _extra = resolved
        assert argv[:3] == ["bin/rails", "server", "-b"]

    def test_the_fallback_capture_spawns_the_override_env(self, monkeypatch, tmp_path) -> None:
        from performer.qa_capture import boot_and_capture_app_screenshot

        seen: dict = {}

        def fake_spawner(cmd, cwd=None, env=None, **kw):
            seen["cmd"], seen["env"] = cmd, env

            class P:
                def terminate(self): pass
                def kill(self): pass
                def wait(self, timeout=None): return 0
                def poll(self): return None

            return P()

        checks = iter([False])

        def port_check(_host, _port):
            return next(checks, True)  # first call: down; everything after: up

        monkeypatch.setattr(
            "performer.qa_capture.capture_app_screenshot", lambda **_kw: "/tmp/shot.png"
        )
        out = boot_and_capture_app_screenshot(
            env={"PORT": "8123", "QA_APP_START_COMMAND": "RAILS_ENV=test bin/rails server"},
            workspace=tmp_path,
            spawner=fake_spawner,
            port_check=port_check,
            sleep=lambda _s: None,
        )

        assert out == "/tmp/shot.png"
        assert "RAILS_ENV" not in seen["cmd"], "the assignment must not become argv[0]"
        assert seen["env"]["RAILS_ENV"] == "test"

    def test_the_fallback_capture_probes_the_command_s_port_override(self, monkeypatch, tmp_path) -> None:
        """411 round-six review: `QA_APP_START_COMMAND='PORT=9000 ...'` spawns
        port 9000, but the loop and capture still probed the outer PORT=8000,
        so the fallback reported no artifact. The effective spawn env decides
        which port is probed and captured."""
        from performer.qa_capture import boot_and_capture_app_screenshot

        seen: dict = {"ports": [], "shell": None, "cmd": None}

        def fake_spawner(cmd, cwd=None, env=None, **kw):
            seen["cmd"], seen["env"], seen["shell"] = cmd, env, kw.get("shell")
            return self._fake_proc()

        checks = iter([False, True, True])

        def port_check(_host, port):
            seen["ports"].append(port)
            return next(checks, True)

        monkeypatch.setattr(
            "performer.qa_capture.capture_app_screenshot", lambda **_kw: "/tmp/shot.png"
        )
        out = boot_and_capture_app_screenshot(
            env={"PORT": "8000", "QA_APP_START_COMMAND": "PORT=9000 python app.py"},
            workspace=tmp_path,
            spawner=fake_spawner,
            port_check=port_check,
            sleep=lambda _s: None,
        )

        assert out == "/tmp/shot.png"
        assert seen["ports"][0] == "9000", "the override's port is what gets probed"
        assert seen["env"]["PORT"] == "9000"

    def test_a_shell_semantic_override_spawns_a_shell(self, monkeypatch, tmp_path) -> None:
        """411 round-six review: `cd web && npm start` is shell-executed by
        the workflow boot but the fallback shlex.split it, so pipelines and
        substitutions worked at boot and failed at the capture. The override
        keeps its shell semantics in both paths."""
        from performer.qa_capture import boot_and_capture_app_screenshot

        seen: dict = {"shell": None, "cmd": None}

        def fake_spawner(cmd, cwd=None, env=None, **kw):
            seen["cmd"], seen["shell"] = cmd, kw.get("shell")
            return self._fake_proc()

        checks = iter([False, True, True])
        monkeypatch.setattr(
            "performer.qa_capture.capture_app_screenshot", lambda **_kw: "/tmp/shot.png"
        )
        out = boot_and_capture_app_screenshot(
            env={"PORT": "8123", "QA_APP_START_COMMAND": "cd web && npm start"},
            workspace=tmp_path,
            spawner=fake_spawner,
            port_check=lambda _h, _p: next(checks, True),
            sleep=lambda _s: None,
        )

        assert out == "/tmp/shot.png"
        assert seen["shell"] is True, "an operator override with shell ops gets a shell"
        assert seen["cmd"] == "cd web && npm start"

    def test_a_redirection_override_spawns_a_shell(self, monkeypatch, tmp_path) -> None:
        """411 round-eight review: `python app.py > /tmp/app.log` is shell-
        executed by the workflow boot (AppBoot grants every override a
        shell) but the fallback's ops regex missed redirection, so the
        redirect ran as argv and the capture booted nothing."""
        from performer.qa_capture import boot_and_capture_app_screenshot

        seen: dict = {"shell": None, "cmd": None}

        def fake_spawner(cmd, cwd=None, env=None, **kw):
            seen["cmd"], seen["shell"] = cmd, kw.get("shell")
            return self._fake_proc()

        checks = iter([False, True, True])
        monkeypatch.setattr(
            "performer.qa_capture.capture_app_screenshot", lambda **_kw: "/tmp/shot.png"
        )
        out = boot_and_capture_app_screenshot(
            env={"PORT": "8123", "QA_APP_START_COMMAND": "python app.py > /tmp/app.log"},
            workspace=tmp_path,
            spawner=fake_spawner,
            port_check=lambda _h, _p: next(checks, True),
            sleep=lambda _s: None,
        )

        assert out == "/tmp/shot.png"
        assert seen["shell"] is True, (
            "redirection is shell semantics; the fallback must agree with the boot"
        )
        assert seen["cmd"] == "python app.py > /tmp/app.log"

    def test_an_unparseable_start_command_falls_back_to_inference(self, tmp_path) -> None:
        """411 round-six review: an unmatched quote made shlex.split raise
        after the workflow returned its report, aborting post-processing
        instead of treating the screenshot as unavailable."""
        from performer.qa_capture import resolve_start_command
        from performer.workflows.project_shape import ProjectShape

        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "rails").write_text("#!/bin/sh\n")
        resolved = resolve_start_command(
            tmp_path,
            {"PORT": "8000", "QA_APP_START_COMMAND": "python app.py --flag 'unmatched"},
            ProjectShape(start_command=""),
        )

        assert resolved is not None
        argv, _extra = resolved
        assert argv[:2] == ["bin/rails", "server"], (
            "an unparseable override falls through to framework inference"
        )

    def test_the_shape_command_parses_env_assignments_like_the_boot(self, monkeypatch, tmp_path) -> None:
        """411 round-six review: a shape command with a valid env prefix such
        as `RAILS_ENV=test bin/rails server` reached AppBoot._default_spawn,
        which shlex.split it and tried to execute `RAILS_ENV=test` as
        argv[0]. The workflow boot and the fallback must parse the same way."""
        from performer.workflows.qa.boot import AppBoot

        seen: dict = {}

        def fake_popen(argv, **kw):
            seen["argv"], seen["env"] = argv, kw.get("env")
            return self._fake_proc()

        monkeypatch.setattr("subprocess.Popen", fake_popen)
        AppBoot._default_spawn(
            "RAILS_ENV=test bin/rails server", tmp_path, {"PORT": "8000"}
        )

        assert seen["argv"] == ["bin/rails", "server"], (
            "the assignment must not become argv[0]"
        )
        assert seen["env"]["RAILS_ENV"] == "test"
        assert seen["env"]["PORT"] == "8000"

    def test_an_unparseable_shell_override_boots_the_inferred_command(self, monkeypatch, tmp_path) -> None:
        """411 round-eight review: use_shell was recomputed from the raw
        override even when the override failed to parse and resolution fell
        back to framework inference — the fallback then spawned the broken
        override string, shell ops and all, instead of the inferred
        command."""
        from performer.qa_capture import boot_and_capture_app_screenshot

        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "rails").write_text("#!/bin/sh\n")
        seen: dict = {"shell": None, "cmd": None}

        def fake_spawner(cmd, cwd=None, env=None, **kw):
            seen["cmd"], seen["shell"] = cmd, kw.get("shell")
            return self._fake_proc()

        checks = iter([False, True, True])
        monkeypatch.setattr(
            "performer.qa_capture.capture_app_screenshot", lambda **_kw: "/tmp/shot.png"
        )
        out = boot_and_capture_app_screenshot(
            env={"PORT": "8123",
                 "QA_APP_START_COMMAND": "bin/rails server && echo 'unmatched"},
            workspace=tmp_path,
            spawner=fake_spawner,
            port_check=lambda _h, _p: next(checks, True),
            sleep=lambda _s: None,
        )

        assert out == "/tmp/shot.png"
        assert seen["shell"] is not True, (
            "an unparseable override resolved to inference; the broken string "
            "must not be spawned under a shell"
        )
        assert seen["cmd"][:2] == ["bin/rails", "server"], (
            "the inferred command is what boots, not the broken override"
        )
        assert "unmatched" not in str(seen["cmd"]), (
            "the broken override string must not be spawned"
        )


class TestCaptureDirLifecycle:
    """411 round-eight review: the delete helper and the report builder must
    not crash on, or publish, paths that cannot survive them."""

    def _make_perf(self) -> Performance:
        stand = Stand(path=Path("/tmp/fake"), branch="feat/test")
        stand.git_env = {}
        score = Score(title="Test", repo_url="https://github.com/acme/repo", branch="feat/test")
        return Performance(session_id="sid", stand=stand, score=score, backend=MagicMock(), role="qa")

    def test_a_symlink_loop_capture_dir_is_left_untouched(self, tmp_path) -> None:
        from performer.qa_postprocess import _delete_qa_capture_dir

        a = tmp_path / "qa-visual-a"
        b = tmp_path / "qa-visual-b"
        a.symlink_to(b, target_is_directory=True)
        b.symlink_to(a, target_is_directory=True)

        _delete_qa_capture_dir({"visual_capture_dir": str(a)})  # must not raise

        assert a.is_symlink() and b.is_symlink(), (
            "an unvalidatable path is never touched"
        )

    @pytest.mark.asyncio
    async def test_a_failed_upload_leaves_no_dead_local_path_in_the_report(self) -> None:
        """resolve leaves the container-local path on an upload failure and
        the evidence tree is deleted right after, so a committed report
        naming that path points at a file that no longer exists. The report
        names the failure instead."""
        async def fake_resolve(visual_evidence, **_kw):
            return visual_evidence  # the upload failed: the local path is left

        committed: list[str] = []

        async def fake_commit(stand, path, content, message):
            committed.append(content)

        perf = self._make_perf()
        perf.backend.get_status.return_value = BackendStatus(
            state="done", output=_qa_output(
                passed=True, criteria_checked=1, criteria_passed=1,
                visual_evidence=[{"label": "s", "kind": "screenshot", "path_or_url": "/tmp/x.png"}],
            )
        )
        with patch("performer.main.resolve_visual_evidence_urls", fake_resolve), \
                patch("performer.main.commit_file", fake_commit):
            await handle_status(
                _msg("status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode")
            )

        assert committed, "the qa.md is committed"
        assert ": `/tmp/x.png`" not in committed[0], (
            "the evidence entry must not render the deleted local path as a "
            "reference; the failures list may still name it as a defect"
        )
        assert "upload failed; local capture not retained" in committed[0]
