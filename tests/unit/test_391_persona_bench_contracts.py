"""391: the persona bench sends what production sends, so every task can pass.

Four tasks could not pass on any backend: the security floor refused a payload
with no scan request, the bootstrap refused one with no reviewed environment
contract, the wiki tech_writer task named a missing fixture branch and shared
a label with the other tech_writer task, and the advocate had no task. These
tests drive the bench's payload path through the performer's own admission
checks, with no Docker, network or model.
"""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

persona_bench = pytest.importorskip("persona_bench")
GradeCtx = persona_bench.GradeCtx

REPO_URL = "https://github.com/acme/conductor-bench.git"


def _task(label):
    return next(t for t in persona_bench.TASKS if t.label == label)


def _ctx(status="", parsed=None, fixture=None):
    return GradeCtx(role="r", state="succeeded", status=status, summary=json.dumps(parsed or {}),
                    parsed=parsed or {}, error_code=None, cid=None, repo_subdir="/ws",
                    fixture=fixture or {})


# --------------------------------------------------------------------------
# Tasks and labels.
# --------------------------------------------------------------------------
class TestTasks:
    def test_labels_are_unique_so_results_keep_every_task(self):
        labels = [t.label for t in persona_bench.TASKS]
        assert len(labels) == len(set(labels))

    def test_both_tech_writer_tasks_use_the_tech_writer_performer(self):
        writers = [t for t in persona_bench.TASKS if t.config_key == "tech_writer"]
        assert {t.label for t in writers} == {"tech_writer", "tech_writer_wiki"}
        assert {t.branch for t in writers} == {"feat/techwriter-nodoc", "feat/techwriter-wiki"}

    def test_roles_filter_matches_label_or_performer_key(self):
        picked = persona_bench.select_tasks(persona_bench.TASKS, "tech_writer")
        assert [t.label for t in picked] == ["tech_writer", "tech_writer_wiki"]
        assert [t.label for t in persona_bench.select_tasks(persona_bench.TASKS, "tech_writer_wiki")] == [
            "tech_writer_wiki"]
        assert persona_bench.select_tasks(persona_bench.TASKS, None) == persona_bench.TASKS

    def test_every_persona_has_a_task(self):
        keys = {t.config_key for t in persona_bench.TASKS}
        assert {"assessor", "architect", "implementer", "reviewer", "security", "qa",
                "tech_writer", "closer", "env_bootstrap", "advocate"} <= keys


# --------------------------------------------------------------------------
# Card dispatch: nare rail metadata and the provider key, as production.
# --------------------------------------------------------------------------
class TestNarePayload:
    def test_self_hosted_mode_rides_metadata_and_bearer(self):
        block = {"model": "claude-haiku-5-5", "endpoint_kind": "litellm",
                 "base_url": "https://gateway.example/v1", "auth_token_env": "BENCH_GATEWAY_KEY"}
        ctx = persona_bench.build_card_context(
            _task("reviewer"), backend="nare", repo_url=REPO_URL, branch="b",
            model="claude-haiku-5-5", model_block=block, workflow="")
        payload = persona_bench.build_job_payload(
            ctx, gh_token="ghs", dotenv={"BENCH_GATEWAY_KEY": "sk-gw"})
        meta = payload["metadata"]
        assert meta["endpoint_kind"] == "litellm"
        assert meta["base_url"] == "https://gateway.example/v1"
        assert meta["model"] == "claude-haiku-5-5"
        assert payload["secrets"]["OPENAI_API_KEY"] == "sk-gw"
        assert payload["secrets"]["GITHUB_TOKEN"] == "ghs"
        assert payload["transcript_max_bytes"] == persona_bench.TRANSCRIPT_MAX_BYTES
        assert "sk-gw" not in json.dumps(meta)

    def test_dotenv_overlay_is_restored(self, monkeypatch):
        import os

        monkeypatch.delenv("BENCH_ONLY_IN_DOTENV", raising=False)
        with persona_bench._daemon_env({"BENCH_ONLY_IN_DOTENV": "1"}):
            assert os.environ["BENCH_ONLY_IN_DOTENV"] == "1"
        assert "BENCH_ONLY_IN_DOTENV" not in os.environ

    def test_role_workflow_rides_the_context(self):
        ctx = persona_bench.build_card_context(
            _task("security"), backend="nare", repo_url=REPO_URL, branch="b", model="m",
            workflow="security")
        assert ctx["workflow"] == "security"


# --------------------------------------------------------------------------
# Security: the scan request the floor (#380) requires.
# --------------------------------------------------------------------------
DIFF = (
    "diff --git a/src/bench/users.py b/src/bench/users.py\n"
    "--- a/src/bench/users.py\n+++ b/src/bench/users.py\n"
    "@@ -0,0 +1 @@\n+q = f\"SELECT * FROM users WHERE name = '{name}'\"\n"
)


class TestSecurityScanRequest:
    def test_prose_security_carries_a_request_the_floor_accepts(self):
        from performer.security_floor import _scan_files

        extra = persona_bench.pr_card_context("security", DIFF, ["src/bench/users.py"], "a" * 40)
        request = extra["security_floor"]
        assert request == {"version": 1, "files": ["src/bench/users.py"], "diff_status": "ok",
                           "head_sha": "a" * 40}
        assert _scan_files(request) == ["src/bench/users.py"]
        assert extra["pr_diff_status"] == "injected"
        assert "SELECT" in extra["pr_diff"]

    def test_security_workflow_gets_no_prose_request(self):
        extra = persona_bench.pr_card_context("security", DIFF, ["src/bench/users.py"], "a" * 40,
                                              workflow="security")
        assert "security_floor" not in extra
        assert extra["pr_changed_paths"] == ["src/bench/users.py"]

    def test_unknown_head_leaves_the_request_unbound(self):
        extra = persona_bench.pr_card_context("security", DIFF, ["src/bench/users.py"], None)
        assert extra["security_floor"]["head_sha"] is None

    def test_diff_roles_get_the_diff_and_others_nothing(self):
        assert "pr_diff" in persona_bench.pr_card_context("qa", DIFF, ["src/bench/users.py"], None)
        assert "security_floor" not in persona_bench.pr_card_context("qa", DIFF, ["x"], None)
        assert persona_bench.pr_card_context("architect", DIFF, ["x"], None) == {}

    def test_moved_fixture_pr_is_a_harness_failure(self, monkeypatch):
        monkeypatch.setattr(persona_bench, "_fork_throwaway_branch", lambda *a: "a" * 40)
        monkeypatch.setattr(persona_bench, "_delete_remote_branch", lambda *a: None)
        monkeypatch.setattr(persona_bench, "fetch_pr_diff", lambda url, tok: (DIFF, ["f"], "b" * 40))
        with pytest.raises(persona_bench.BenchSetupError, match="head"):
            persona_bench.prepare_cell(
                _task("security"), backend="nare", repo_url=REPO_URL, gh_token="t",
                dispatch={"model": "m"}, persona=None, prs={"security": "https://github.com/a/b/pull/6"},
                dotenv={}, judge_cfg=None, config_path=None)

    def test_prepared_security_cell_binds_the_fork_head(self, monkeypatch):
        deleted = []
        monkeypatch.setattr(persona_bench, "_fork_throwaway_branch", lambda *a: "a" * 40)
        monkeypatch.setattr(persona_bench, "_delete_remote_branch", lambda *a: deleted.append(a))
        monkeypatch.setattr(persona_bench, "fetch_pr_diff", lambda url, tok: (DIFF, ["src/bench/users.py"], "a" * 40))
        cell = persona_bench.prepare_cell(
            _task("security"), backend="nare", repo_url=REPO_URL, gh_token="t",
            dispatch={"model": "m"}, persona=None, prs={"security": "https://github.com/a/b/pull/6"},
            dotenv={}, judge_cfg=None, config_path=None)
        assert cell.card_context["security_floor"]["head_sha"] == "a" * 40
        assert cell.card_context["branch"].startswith("bench-ro-security-")
        payload = persona_bench.build_job_payload(cell.card_context, gh_token="t")
        assert payload["metadata"]["security_floor"]["files"] == ["src/bench/users.py"]
        for cleanup in cell.cleanups:
            cleanup()
        assert deleted


# --------------------------------------------------------------------------
# env_bootstrap: production discovery and review, then the performer's own
# contract check accepts the payload (#352).
# --------------------------------------------------------------------------
README = "Install: pip install -e .[test]\nRun tests: pytest -q\n"


class ScriptedChat:
    """A discovery model that reads README.md and returns a fixed manifest."""

    def __init__(self, commit):
        self.commit = commit
        self.calls = 0
        self.manifest = {
            "schema_version": 1, "policy_version": "351-v1", "symphony_name": "conductor-bench",
            "source_commit": commit, "inputs": {"README.md": hashlib.sha256(README.encode()).hexdigest()},
            "requirements": [{"id": "pytest", "description": "pytest runs", "source": "README.md",
                              "checks": ["pytest"], "required": True}],
            "install": [], "checks": [{"id": "pytest", "argv": ["python", "-m", "pytest", "--version"],
                                       "required": True}],
            "activation": {"path": [], "env": {}}, "services": [], "empty_reason": None,
        }

    async def __call__(self, messages):
        from coordinare_service_inference.environment.models import EnvironmentManifest

        self.calls += 1
        if self.calls in (1, 3):
            return json.dumps({"tool": "read_file", "path": "README.md"})
        if self.calls == 2:
            return json.dumps({"manifest": copy.deepcopy(self.manifest)})
        return json.dumps({"assessment": {
            "schema_version": 1, "policy_version": "351-v1", "source_commit": self.commit,
            "manifest_digest": EnvironmentManifest.model_validate(self.manifest).digest(),
            "verdict": "accept", "rationale": "README documents pytest", "sources": ["README.md"],
        }})


@pytest.fixture
def snapshot(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text(README)
    (repo / "pyproject.toml").write_text("[project]\nname = 'b'\n")
    env = {"GIT_AUTHOR_NAME": "b", "GIT_AUTHOR_EMAIL": "b@example.invalid",
           "GIT_COMMITTER_NAME": "b", "GIT_COMMITTER_EMAIL": "b@example.invalid", "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "x"], check=True, env=env)
    commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                            capture_output=True, text=True).stdout.strip()
    return repo, commit, ["README.md", "pyproject.toml"]


class TestBootstrapContract:
    def test_bootstrap_payload_passes_the_performer_contract_check(self, snapshot):
        from performer.environment_contract import accepted_manifest

        repo, commit, paths = snapshot
        ctx = persona_bench.bootstrap_card_context(
            repo_dir=repo, commit=commit, paths=paths, repo_url=REPO_URL, chat=ScriptedChat(commit),
            backend="nare", gh_token="ghs", model_block={"model": "m", "endpoint_kind": "litellm"})
        payload = persona_bench.build_job_payload(ctx, gh_token="ghs")
        assert payload["role"] == "env_bootstrap"
        assert payload["env_cache_path"] == persona_bench.BOOTSTRAP_CACHE_PATH
        assert set(payload["bootstrap_scripts"]) == {"verify_sh", "activate_sh"}
        assert payload["secrets"]["GITHUB_TOKEN"] == "ghs"
        assert payload["metadata"]["endpoint_kind"] == "litellm"
        assert "_github_token" not in payload["metadata"]
        manifest = accepted_manifest(payload["metadata"])
        assert manifest.source_commit == commit
        assert payload["repo_url"] == REPO_URL

    def test_discovery_that_does_not_accept_raises(self, snapshot):
        repo, commit, paths = snapshot
        chat = ScriptedChat(commit)
        original = chat.__call__

        async def revise(messages):
            reply = await original(messages)
            return reply.replace('"accept"', '"revise"')

        with pytest.raises(Exception, match="revise"):
            persona_bench.bootstrap_card_context(
                repo_dir=repo, commit=commit, paths=paths, repo_url=REPO_URL, chat=revise,
                backend="nare", gh_token="ghs")

    def test_no_discovery_model_is_a_harness_failure(self, monkeypatch):
        monkeypatch.setattr(persona_bench, "discovery_chat", lambda *a: None)
        with pytest.raises(persona_bench.BenchSetupError, match="discovery model"):
            persona_bench.prepare_cell(
                _task("env_bootstrap"), backend="nare", repo_url=REPO_URL, gh_token="t",
                dispatch={"model": "m"}, persona=None, prs={}, dotenv={}, judge_cfg=None,
                config_path=None)

    def test_grader_needs_the_manifest_checks_to_pass(self):
        ok_parsed = {"status": "env_bootstrap_complete",
                     "report": {"environment_verification": {"passed": True, "results": [{}]}}}
        ok, markers, _ = persona_bench.grade_env_bootstrap(_ctx("env_bootstrap_complete", ok_parsed))
        assert ok and markers == ["manifest_checks_passed"]
        bad = {"status": "env_bootstrap_complete", "report": {"environment_verification": {"passed": False}}}
        assert persona_bench.grade_env_bootstrap(_ctx("env_bootstrap_complete", bad))[0] is False

    def test_bootstrap_cell_gets_no_gateway_env(self):
        env = persona_bench._container_env({"env": {"BACKEND": "nare"}}, {}, _task("env_bootstrap"), "nare")
        assert "LITELLM_PROXY_BASE_URL" not in " ".join(env)


# --------------------------------------------------------------------------
# Advocate: card-less intake dispatch, per-run fixture issues, grader.
# --------------------------------------------------------------------------
class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class TestAdvocate:
    def test_payload_is_the_intake_dispatch(self):
        ctx = persona_bench.advocate_card_context(
            repo_url=REPO_URL, backend="nare", persona="be the advocate",
            model_block={"model": "m", "endpoint_kind": "litellm"})
        assert ctx["role"] == "advocate"
        assert ctx["workflow"] == "advocate"
        assert ctx["id"] == "advocate-conductor-bench"
        assert json.loads(ctx["workflow_env"]["ADVOCATE_DOC_SOURCES"]) == ["README.md"]
        payload = persona_bench.build_job_payload(ctx, gh_token="ghs")
        assert payload["role"] == "advocate"
        assert payload["card_id"] == "advocate-conductor-bench"
        assert payload["metadata"]["workflow"] == "advocate"
        assert payload["secrets"]["GITHUB_TOKEN"] == "ghs"

    def test_fixture_issues_open_and_close(self, monkeypatch):
        calls = []

        def api(method, path, token, json_body=None):
            calls.append((method, path, json_body))
            if method == "POST":
                return _Resp(201, {"number": 10 + len(calls)})
            return _Resp(200)

        monkeypatch.setattr(persona_bench, "_gh_api", api)
        numbers = persona_bench.open_advocate_issues(REPO_URL, "t")
        assert set(numbers) == {"answerable", "unanswerable"}
        persona_bench.close_advocate_issues(REPO_URL, "t", numbers)
        closed = [c for c in calls if c[0] == "PATCH"]
        assert {c[1] for c in closed} == {f"/repos/acme/conductor-bench/issues/{n}" for n in numbers.values()}
        assert all(c[2]["state"] == "closed" for c in closed)

    def test_a_failed_open_closes_what_it_opened(self, monkeypatch):
        calls = []

        def api(method, path, token, json_body=None):
            calls.append(method)
            if method == "POST":
                return _Resp(201, {"number": 5}) if calls.count("POST") == 1 else _Resp(403)
            return _Resp(200)

        monkeypatch.setattr(persona_bench, "_gh_api", api)
        with pytest.raises(RuntimeError):
            persona_bench.open_advocate_issues(REPO_URL, "t")
        assert calls.count("PATCH") == 1

    def test_prepared_cell_closes_its_issues(self, monkeypatch):
        closed = []
        monkeypatch.setattr(persona_bench, "open_advocate_issues", lambda *a: {"answerable": 1, "unanswerable": 2})
        monkeypatch.setattr(persona_bench, "close_advocate_issues", lambda url, tok, nums: closed.append(nums))
        cell = persona_bench.prepare_cell(
            _task("advocate"), backend="nare", repo_url=REPO_URL, gh_token="t",
            dispatch={"model": "m"}, persona=None, prs={}, dotenv={}, judge_cfg=None, config_path=None)
        assert cell.fixture == {"answerable": 1, "unanswerable": 2}
        for cleanup in cell.cleanups:
            cleanup()
        assert closed == [{"answerable": 1, "unanswerable": 2}]

    def test_grader_reads_the_outcome_record(self):
        def parsed(a, b):
            return {"status": "advocate_complete", "report": {"advocate": {"outcomes": [
                {"number": 1, "action": a}, {"number": 2, "action": b}]}}}
        fixture = {"answerable": 1, "unanswerable": 2}
        ok, mk, _ = persona_bench.grade_advocate(_ctx("advocate_complete", parsed("replied", "escalated"), fixture))
        assert ok and mk == ["answered_documented_question", "escalated_undocumented_question"]
        ok, mk, detail = persona_bench.grade_advocate(_ctx("advocate_complete", parsed("replied", "replied"), fixture))
        assert ok and mk == [] and "ANSWERED_WITHOUT_DOCS" in detail
        assert persona_bench.grade_advocate(_ctx("env_blocked", {}, fixture))[0] is False

    def test_gateway_env_for_the_workflow_only(self):
        dotenv = {"COORDINARE_INFERENCE_BASE_URL": "https://gateway.example/v1", "LITELLM_MASTER_KEY": "k"}
        env = persona_bench._container_env({"env": {"BACKEND": "nare"}}, dotenv, _task("advocate"), "nare")
        assert "LITELLM_PROXY_BASE_URL=https://gateway.example/v1" in env
        assert "LITELLM_PROXY_AUTH_TOKEN=k" in env
        configured = persona_bench._container_env(
            {"env": {"BACKEND": "nare", "LITELLM_PROXY_BASE_URL": "https://own.example"}}, dotenv,
            _task("advocate"), "nare")
        assert "LITELLM_PROXY_BASE_URL=https://own.example" in configured
        assert not any(e.startswith("LITELLM_PROXY") for e in persona_bench._container_env(
            {"env": {"BACKEND": "nare"}}, dotenv, _task("qa"), "nare"))


# --------------------------------------------------------------------------
# Raw backend output beside the cell JSON.
# --------------------------------------------------------------------------
class TestRawOutput:
    def test_transcript_is_read_in_batches_until_empty(self, monkeypatch):
        records = [{"seq": i, "type": "assistant_text", "text": f"t{i}"} for i in range(1, 6)]
        seen = []

        class R:
            status_code = 200

            def __init__(self, batch):
                self._batch = batch

            def json(self):
                return {"transcript": self._batch}

        def get(url, params=None, timeout=None):
            after, limit = params["transcript_after"], params["transcript_limit"]
            seen.append(after)
            return R([r for r in records if r["seq"] > after][:limit])

        monkeypatch.setattr(persona_bench.httpx, "get", get)
        out = persona_bench.fetch_transcript("http://x", "j", limit=2)
        assert [r["seq"] for r in out] == [1, 2, 3, 4, 5]
        assert seen == [0, 2, 4, 5]

    def test_cell_and_raw_files_sit_side_by_side(self, tmp_path):
        cell, raw = persona_bench.write_cell_files(
            tmp_path, "qa", "nare-ephemeral", {"row": {"role": "qa"}}, [{"seq": 1, "text": "{}"}])
        assert cell == "cells/qa__nare-ephemeral.json"
        assert raw == "cells/qa__nare-ephemeral.raw.jsonl"
        assert json.loads((tmp_path / cell).read_text())["raw_file"] == raw
        assert json.loads((tmp_path / raw).read_text().splitlines()[0])["text"] == "{}"


# --------------------------------------------------------------------------
# One whole cell, with Docker and the performer faked.
# --------------------------------------------------------------------------
class TestRunCell:
    def test_qa_cell_posts_the_production_payload_and_saves_raw_output(self, monkeypatch, tmp_path):
        posted = {}
        docker_calls = []
        summary = json.dumps({"status": "qa_failed", "failures": [{"actual": "KeyError: 'discount'"}],
                              "report": {"verification_steps": ["FAIL: KeyError"]}})

        class Proc:
            def __init__(self, stdout=""):
                self.returncode, self.stdout, self.stderr = 0, stdout, ""

        def run(argv, **kw):
            docker_calls.append(argv)
            return Proc("cid123\n" if argv[:2] == ["docker", "run"] else "")

        class Resp:
            def __init__(self, code, payload=None):
                self.status_code, self._payload, self.text = code, payload or {}, ""

            def json(self):
                return self._payload

        def get(url, params=None, timeout=None):
            if url.endswith("/healthz"):
                return Resp(200)
            if params:
                batch = [{"seq": 1, "type": "assistant_text", "text": summary}]
                return Resp(200, {"transcript": [r for r in batch if r["seq"] > params["transcript_after"]]})
            return Resp(200, {"state": "succeeded", "result": {"summary": summary}})

        def post(url, json=None, timeout=None):
            posted.update(json)
            return Resp(202)

        monkeypatch.setattr(persona_bench.subprocess, "run", run)
        monkeypatch.setattr(persona_bench, "_docker_exec", lambda *a, **k: "")
        monkeypatch.setattr(persona_bench.httpx, "get", get)
        monkeypatch.setattr(persona_bench.httpx, "post", post)
        monkeypatch.setattr(persona_bench.time, "sleep", lambda s: None)
        monkeypatch.setattr(persona_bench, "_fork_throwaway_branch", lambda *a: "a" * 40)
        monkeypatch.setattr(persona_bench, "_delete_remote_branch", lambda *a: None)
        monkeypatch.setattr(persona_bench, "fetch_pr_diff", lambda url, tok: (DIFF, ["src/bench/users.py"], "a" * 40))
        monkeypatch.setattr(persona_bench, "_gh_token", lambda dotenv: "ghs")

        row = persona_bench.run_cell(
            _task("qa"), {"id": "nare-ephemeral", "env": {"BACKEND": "nare"}}, {}, {}, REPO_URL,
            {"qa": "https://github.com/acme/conductor-bench/pull/3"}, None, tmp_path)

        assert posted["role"] == "qa"
        assert posted["job_id"]
        assert posted["metadata"]["pr_diff_status"] == "injected"
        assert posted["transcript_max_bytes"] == persona_bench.TRANSCRIPT_MAX_BYTES
        assert row["status"] == "qa_failed"
        assert row["raw_file"] == "cells/qa__nare-ephemeral.raw.jsonl"
        assert (tmp_path / row["raw_file"]).read_text().count("\n") == 1
        assert (tmp_path / row["log_file"]).is_file()
        assert docker_calls[-1][:3] == ["docker", "rm", "-f"]



# --------------------------------------------------------------------------
# Review round 1.
# --------------------------------------------------------------------------
class TestReviewRound1:
    def test_failed_clone_never_carries_the_token(self, monkeypatch, tmp_path):
        def run(argv, **kw):
            raise subprocess.CalledProcessError(128, argv, stderr="fatal: auth failed for ghs_SECRET")

        monkeypatch.setattr(persona_bench.subprocess, "run", run)
        with pytest.raises(RuntimeError) as err:
            persona_bench.clone_snapshot(REPO_URL, "ghs_SECRET", tmp_path / "r")
        assert "ghs_SECRET" not in str(err.value)
        assert "fatal: auth failed" in str(err.value)
        assert err.value.__cause__ is None

    def test_clone_puts_the_token_in_env_not_argv(self, monkeypatch, tmp_path):
        seen = []

        class P:
            stdout = "abc\0"

        def run(argv, **kw):
            seen.append((argv, kw.get("env") or {}))
            return P()

        monkeypatch.setattr(persona_bench.subprocess, "run", run)
        persona_bench.clone_snapshot(REPO_URL, "ghs_SECRET", tmp_path / "r")
        import base64

        clone_argv, clone_env = seen[0]
        assert "ghs_SECRET" not in " ".join(clone_argv)
        assert clone_env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
        header = clone_env["GIT_CONFIG_VALUE_0"]
        assert header.startswith("Authorization: Basic ")
        assert base64.b64decode(header.split()[-1]).decode() == "x-access-token:ghs_SECRET"

    def test_paths_with_spaces_survive_the_inventory(self, snapshot):
        repo, _commit, _paths = snapshot
        (repo / "docs").mkdir()
        (repo / "docs" / "Getting Started.md").write_text("x")
        env = {"GIT_AUTHOR_NAME": "b", "GIT_AUTHOR_EMAIL": "b@example.invalid",
               "GIT_COMMITTER_NAME": "b", "GIT_COMMITTER_EMAIL": "b@example.invalid", "PATH": "/usr/bin:/bin"}
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "y"], check=True, env=env)
        _c, paths = persona_bench.clone_snapshot(str(repo), "", repo.parent / "clone")
        assert "docs/Getting Started.md" in paths

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        for key in ("LITELLM_MASTER_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GW_KEY", "QA_BASE_URL"):
            monkeypatch.delenv(key, raising=False)

    def test_pre_080_gateway_endpoint_keeps_the_master_key(self):
        ctx = persona_bench.build_card_context(
            _task("reviewer"), backend="claude_code", repo_url=REPO_URL, branch="b", model="m")
        payload = persona_bench.build_job_payload(
            ctx, gh_token="t", dotenv={"LITELLM_MASTER_KEY": "sk-master", "ANTHROPIC_API_KEY": "sk-vendor"})
        assert payload["secrets"]["ANTHROPIC_AUTH_TOKEN"] == "sk-master"
        assert "ANTHROPIC_API_KEY" not in payload["secrets"]
        codex = persona_bench.build_job_payload(
            {**ctx, "backend": "codex"}, gh_token="t",
            dotenv={"LITELLM_MASTER_KEY": "sk-master", "OPENAI_API_KEY": "sk-vendor"})
        assert codex["secrets"]["OPENAI_API_KEY"] == "sk-master"

    def test_a_native_mode_keeps_the_vendor_key(self):
        ctx = persona_bench.build_card_context(
            _task("reviewer"), backend="claude_code", repo_url=REPO_URL, branch="b", model="m",
            model_block={"model": "m", "endpoint_kind": "anthropic"})
        payload = persona_bench.build_job_payload(
            ctx, gh_token="t", dotenv={"LITELLM_MASTER_KEY": "sk-master", "ANTHROPIC_API_KEY": "sk-vendor"})
        assert payload["secrets"]["ANTHROPIC_API_KEY"] == "sk-vendor"
        assert "ANTHROPIC_AUTH_TOKEN" not in payload["secrets"]

    def test_a_routed_mode_keeps_the_production_secrets(self):
        ctx = persona_bench.build_card_context(
            _task("reviewer"), backend="codex", repo_url=REPO_URL, branch="b", model="m",
            model_block={"base_url": "https://gw.example/v1", "auth_token_env": "GW_KEY"})
        payload = persona_bench.build_job_payload(
            ctx, gh_token="t", dotenv={"LITELLM_MASTER_KEY": "sk-master", "GW_KEY": "sk-gw"})
        assert payload["secrets"]["OPENAI_API_KEY"] == "sk-gw"

    def test_role_tuning_and_workflow_env_ride_the_context(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "github_org: o\nhuman_reviewers: [a]\ngithub_auth: pat\ngithub_token: ghp_x\n"
            "endpoints:\n  - {name: gw, kind: litellm, base_url: 'https://gw.example', auth_env: GW_KEY}\n"
            "model_endpoints:\n  - {name: m1, endpoint: gw, model: m-1}\n"
            "modes:\n  - {name: solo, strategy: single, tool: m1}\n"
            "performers:\n  default: {backend: nare, mode: solo}\n"
            "  qa: {backend: nare, mode: solo, effort: high, workflow: qa,"
            " workflow_env: {QA_X: '1', QA_BASE_URL: '${QA_BASE_URL}'}}\n"
            "  env_bootstrap: {backend: claude_code, mode: solo, effort: high, temperature: 0.2}\n")
        out = persona_bench.resolve_role_dispatch(str(cfg), "qa", backend="nare",
                                                  dotenv={"QA_BASE_URL": "https://qa.example"})
        assert out["workflow"] == "qa"
        assert out["workflow_env"] == {"QA_X": "1", "QA_BASE_URL": "https://qa.example"}
        assert out["tuning"].get("effort") == "high"
        # Tuning is translated for the backend the cell runs, not the config's.
        claude = persona_bench.resolve_role_dispatch(str(cfg), "qa", backend="claude_code")
        assert "effort" not in claude["tuning"]
        assert claude["tuning"].get("thinking")
        # The bootstrap reads the raw effort and temperature, as the daemon does.
        boot = persona_bench.resolve_role_dispatch(str(cfg), "env_bootstrap", backend="claude_code")
        assert boot["effort"] == "high"
        assert boot["temperature"] == 0.2
        ctx = persona_bench.build_card_context(
            _task("qa"), backend="nare", repo_url=REPO_URL, branch="b", model=out["model"],
            model_block=out["model_block"], workflow=out["workflow"],
            workflow_env=out["workflow_env"], tuning=out["tuning"])
        assert ctx["workflow_env"] == {"QA_X": "1", "QA_BASE_URL": "https://qa.example"}
        assert ctx["effort"] == "high"
        assert ctx["endpoint_kind"] == "litellm"

    def test_a_bad_advocate_repo_is_a_harness_failure(self):
        with pytest.raises(persona_bench.BenchSetupError):
            persona_bench.prepare_cell(
                _task("advocate"), backend="nare", repo_url="git@example.com:o/r.git", gh_token="t",
                dispatch={"model": "m"}, persona=None, prs={}, dotenv={}, judge_cfg=None, config_path=None)


# --------------------------------------------------------------------------
# Copilot review round.
# --------------------------------------------------------------------------
class TestCopilotRound:
    def test_answering_the_undocumented_issue_is_not_a_pass_without_a_judge(self):
        parsed = {"status": "advocate_complete", "report": {"advocate": {"outcomes": [
            {"number": 1, "action": "replied"}, {"number": 2, "action": "replied"}]}}}
        ok, mk, _ = persona_bench.grade_advocate(
            _ctx("advocate_complete", parsed, {"answerable": 1, "unanswerable": 2}))
        assert persona_bench.categorize("succeeded", "", ok, mk, None)[0] == "FAIL_MODEL"

    def test_the_judge_still_decides_an_unexpected_but_sensible_outcome(self):
        parsed = {"status": "advocate_complete", "report": {"advocate": {"outcomes": [
            {"number": 1, "action": "replied"}, {"number": 2, "action": "redirected"}]}}}
        ok, mk, _ = persona_bench.grade_advocate(
            _ctx("advocate_complete", parsed, {"answerable": 1, "unanswerable": 2}))
        assert persona_bench.categorize("succeeded", "", ok, mk, True)[0] == "PASS"

    def test_a_wrong_pair_with_a_harness_word_is_still_a_model_miss(self):
        parsed = {"status": "advocate_complete", "report": {"advocate": {"outcomes": [
            {"number": 1, "action": "escalated"}, {"number": 2, "action": "escalated"}]}}}
        ok, mk, _ = persona_bench.grade_advocate(
            _ctx("advocate_complete", parsed, {"answerable": 1, "unanswerable": 2}))
        assert persona_bench.categorize("succeeded", "support is unreachable", ok, mk, None)[0] == "FAIL_MODEL"

    def test_escalating_the_documented_issue_is_not_a_pass(self):
        parsed = {"status": "advocate_complete", "report": {"advocate": {"outcomes": [
            {"number": 1, "action": "escalated"}, {"number": 2, "action": "escalated"}]}}}
        ok, mk, _ = persona_bench.grade_advocate(
            _ctx("advocate_complete", parsed, {"answerable": 1, "unanswerable": 2}))
        assert persona_bench.categorize("succeeded", "", ok, mk, None)[0] == "FAIL_MODEL"

    def test_discovery_chat_reads_conducting_placeholders_from_dotenv(self, tmp_path, monkeypatch):
        import coordinare.services.conducting as conducting

        monkeypatch.delenv("BENCH_GW_URL", raising=False)
        seen = {}

        class FakeBackend:
            def __init__(self, **kw):
                seen.update(kw)

            async def chat_json(self, messages):
                return "{}"

        monkeypatch.setattr(conducting, "OpenAiApiBackend", FakeBackend)
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "github_org: o\nhuman_reviewers: [a]\ngithub_auth: pat\ngithub_token: ghp_x\n"
            "conducting: {backend: openai_api, base_url: '${BENCH_GW_URL}', model: m, api_key_env: GW_KEY}\n")
        chat = persona_bench.discovery_chat(str(cfg), {"BENCH_GW_URL": "https://gw.example/v1", "GW_KEY": "k"}, None)
        assert chat is not None
        assert seen["base_url"] == "https://gw.example/v1"
        assert seen["api_key"] == "k"

    def test_a_config_that_fails_to_load_says_so(self, tmp_path, capsys):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("not: [valid\n")
        assert persona_bench.discovery_chat(str(cfg), {}, None) is None
        assert "could not load" in capsys.readouterr().out

    def test_advocate_config_reads_placeholders_from_dotenv(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BENCH_SUPPORT_URL", raising=False)
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "github_org: o\nhuman_reviewers: [a]\ngithub_auth: pat\ngithub_token: ghp_x\n"
            "advocate: {support_channel_url: '${BENCH_SUPPORT_URL}'}\n")
        ctx = persona_bench.advocate_card_context(
            repo_url=REPO_URL, backend="nare", persona=None, config_path=str(cfg),
            dotenv={"BENCH_SUPPORT_URL": "https://support.example"})
        assert ctx["workflow_env"]["ADVOCATE_SUPPORT_URL"] == "https://support.example"
