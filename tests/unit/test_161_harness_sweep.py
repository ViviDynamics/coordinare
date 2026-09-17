"""Spec 161 — a failing harness point is recorded, and the sweep continues.

T024 (US2): FR-018. Deliberately does NOT depend on the US1 classifier: classifying a
failure as a harness defect rather than a legitimate negative verdict is the rollup's job.
This story only requires the failure be *recorded and attributable*, which is what keeps
US2 independently shippable.

Finding worth recording: the continue-on-failure half of FR-018 was ALREADY implemented
by spec 136 (`sweep._run_point` catches Exception with the comment "a failed point is
data, not a crash"). These tests pin that behavior against regression rather than
introducing it, and confirm the failure is attributable to the swept harness.
"""

from __future__ import annotations

import shutil

import pytest
import yaml

from coordinare.bench.space import load_space
from coordinare.bench.sweep import PointResult, enumerate_ablation, rank_candidates

BASELINE = "benchmarks/spaces/baseline.yaml"


def _space_file(tmp_path, dimensions: list[dict]) -> str:
    shutil.copy(BASELINE, tmp_path / "baseline.yaml")
    path = tmp_path / "space.yaml"
    path.write_text(
        yaml.safe_dump({
            "name": "harness-sweep-test",
            "baseline_config": "baseline.yaml",
            "dimensions": dimensions,
        }),
    )
    return str(path)


def test_harness_points_are_attributable_to_their_harness(tmp_path) -> None:
    """FR-018: each point names the dimension and the harness value it exercised, so a
    failure can be pinned on the harness that caused it."""
    loaded = load_space(
        _space_file(tmp_path, [
            {"name": "reviewer-harness", "role": "reviewer",
             "choices": ["openclaw", "claude_code", "junie"]},
        ]),
    )
    specs = enumerate_ablation(loaded)

    harness_specs = [s for s in specs if s.dimension == "reviewer-harness"]
    assert {s.value for s in harness_specs} == {"openclaw", "claude_code", "junie"}
    for s in harness_specs:
        # The override is what makes the point attributable to one harness.
        assert s.overrides == {"global_config.performers.reviewer.backend": s.value}


def test_one_point_per_declared_harness(tmp_path) -> None:
    """FR-014/FR-015: expansion is one point per choice, not a cross-product."""
    loaded = load_space(
        _space_file(tmp_path, [
            {"name": "reviewer-harness", "role": "reviewer",
             "choices": ["openclaw", "claude_code"]},
            {"name": "security-harness", "role": "security",
             "choices": ["openclaw", "codex"]},
        ]),
    )
    specs = [s for s in enumerate_ablation(loaded) if s.dimension]

    # Ablation, not cross-product: 2 + 2 points, never 4 combinations.
    assert len(specs) == 4
    assert all(len(s.overrides) == 1 for s in specs)


def test_a_failed_point_is_recorded_as_data_not_raised() -> None:
    """FR-018: the sweep must not abort. A failed point carries its error and is retained
    so the harness that produced it is still visible in the results."""
    failed = PointResult(
        point_id="reviewer-harness=junie", kind="ablation",
        dimension="reviewer-harness", value="junie",
        failed=True, error="RuntimeError: harness could not start",
    )
    ok = PointResult(
        point_id="reviewer-harness=openclaw", kind="ablation",
        dimension="reviewer-harness", value="openclaw", mean_scalar=0.9,
    )

    # Both survive into the result set; the failure is data.
    assert failed.failed and failed.error
    assert failed.value == "junie", "the failing harness must stay attributable"
    assert not ok.failed


def test_a_failed_harness_does_not_outrank_a_working_one() -> None:
    """FR-018: a failed point must never be ranked above a real result. It sorts last as
    unrankable rather than being treated as a zero score that might beat a poor one."""
    good = PointResult(point_id="p-openclaw", kind="candidate", mean_scalar=0.10)
    failed = PointResult(point_id="p-junie", kind="candidate", failed=True, error="boom")

    ranked = rank_candidates([failed, good])

    assert ranked.index("p-openclaw") < ranked.index("p-junie")


def test_an_unrankable_point_is_not_confused_with_a_zero_score() -> None:
    """The absent-vs-zero rule again, at the sweep level: a point with no scalar is
    unrankable, not a measured 0.0."""
    no_score = PointResult(point_id="p-none", kind="candidate", mean_scalar=None)
    zero = PointResult(point_id="p-zero", kind="candidate", mean_scalar=0.0)

    ranked = rank_candidates([no_score, zero])

    # The measured zero is rankable; the missing scalar is not, so it sorts last.
    assert ranked.index("p-zero") < ranked.index("p-none")


def test_sweep_point_failure_handling_still_exists() -> None:
    """Pin spec 136's continue-on-failure contract, which FR-018 relies on.

    If someone removes the broad catch in `_run_point`, one bad harness would abort an
    entire sweep and discard every earlier point's spent budget. That regression would
    otherwise only show up in a long live run.
    """
    import inspect

    from coordinare.bench import sweep

    src = inspect.getsource(sweep._run_point)
    assert "except Exception" in src, (
        "_run_point must keep catching per-point failures; without it a single failing "
        "harness aborts the whole sweep (FR-018)"
    )
    assert "result.failed = True" in src, "a failed point must be recorded, not dropped"


@pytest.mark.asyncio
async def test_real_sweep_passes_each_selected_backend_to_runner(tmp_path, monkeypatch):
    from coordinare.bench import sweep
    from coordinare.bench.runner import run_board

    loaded = load_space(_space_file(tmp_path, [
        {"name": "reviewer-harness", "role": "reviewer", "choices": ["claude_code", "codex"]},
    ]))
    observed = []

    async def capture(fixtures, run_dir, **kwargs):
        assert kwargs["stub"] is False
        assert kwargs["wall_clock_budget_seconds"] == 17
        real = kwargs.pop("real_config")
        assert real is not None
        observed.append(real.performers.resolved_role("reviewer").backend)
        return await run_board(fixtures, run_dir, **{**kwargs, "stub": True})

    monkeypatch.setattr(sweep, "run_board", capture)
    result = await sweep.run_sweep(loaded, "ablation", tmp_path / "runs", stub=False,
                                   wall_clock_budget_seconds=17)
    assert observed == ["opencode", "claude_code", "codex"]
    assert result.coverage.scored_points == 3
    assert not result.coverage.dropped


def test_real_point_splits_shared_endpoint_without_mutating_baseline(tmp_path):
    from coordinare.bench.sweep import _real_point_config
    from coordinare.models.performer_endpoint import PerformerEndpointConfig

    cfg = load_space(_space_file(tmp_path, [
        {"name": "reviewer-harness", "role": "reviewer", "choices": ["claude_code"]},
    ])).baseline.global_config
    cfg.performers.reviewer.backend = "claude_code"
    ep = PerformerEndpointConfig(
        id="shared", mode="ephemeral", roles=["implementer", "reviewer"],
        image="coordinare-performer:full", env={"BACKEND": "opencode", "KEEP": "yes"},
        extra_hosts=["host.docker.internal:host-gateway"],
    )
    cfg.performer_endpoints = [ep]
    result = _real_point_config(cfg)
    assert len(result.performer_endpoints) == 2
    assert len({e.id for e in result.performer_endpoints}) == 2
    for endpoint in result.performer_endpoints:
        assert endpoint.env["BACKEND"] == result.performers.resolved_role(endpoint.roles[0]).backend
        assert endpoint.env["KEEP"] == "yes"
        assert endpoint.image == ep.image
        assert endpoint.extra_hosts == ep.extra_hosts
    assert ep.roles == ["implementer", "reviewer"]
    assert ep.env == {"BACKEND": "opencode", "KEEP": "yes"}
    assert cfg.performer_endpoints == [ep]


def test_real_point_rejects_mismatched_persistent_harness(tmp_path):
    from coordinare.bench.sweep import _real_point_config
    from coordinare.models.performer_endpoint import PerformerEndpointConfig

    cfg = load_space(_space_file(tmp_path, [
        {"name": "reviewer-harness", "role": "reviewer", "choices": ["claude_code"]},
    ])).baseline.global_config
    cfg.performer_endpoints = [PerformerEndpointConfig(
        id="fixed", mode="persistent", roles=["reviewer"], endpoint="http://127.0.0.1:9000",
        image="coordinare-performer:full",
        env={"BACKEND": "claude_code"},
    )]
    with pytest.raises(ValueError, match="must be ephemeral"):
        _real_point_config(cfg)


@pytest.mark.asyncio
async def test_real_root_config_retains_session_between_cycles(tmp_path):
    from coordinare.bench.recording_performer import RecordingPerformer
    from coordinare.bench.runner import _no_sleep, run_board
    from coordinare.config import CoordinareConfiguration
    from tests.unit.test_151_runner_failure import _STAGES, _config, _StubServer

    class Working:
        dispatches = 0
        polls = 0

        async def check_health(self):
            return {"status": "ready"}

        async def dispatch_card(self, card_context, **kwargs):
            self.dispatches += 1
            return {"status": "ok", "session_id": "inflight"}

        def has_live_session(self, session_id):
            return session_id == "inflight"

        async def check_status(self, session_id, **kwargs):
            self.polls += 1
            return {"status": "working"}

    service = Working()
    recorder = RecordingPerformer(service)
    cfg = CoordinareConfiguration(global_config=_config(), symphonies=[
        {"name": "bench", "github_project_number": 1},
    ])
    from coordinare.bench.fixtures import tiny_fixture
    await run_board([tiny_fixture()], tmp_path / "run", stub=False, max_cycles=4,
                    config=cfg, real_config=cfg.global_config, server=_StubServer(),
                    performer_services=dict.fromkeys(_STAGES, recorder), sleep_func=_no_sleep)
    assert service.dispatches == 1, "root configuration must not reset the live session each cycle"
    assert service.polls >= 2


@pytest.mark.asyncio
async def test_fake_git_server_stops_children_and_releases_listener(tmp_path):
    import asyncio
    import socket

    from coordinare.bench.fake_github_server import FakeGitHubServer
    from coordinare.bench.fixtures import materialize_repo, tiny_fixture
    from coordinare.services.fake_github import FakeGitHubService

    bare = materialize_repo([tiny_fixture()], tmp_path / "repos")
    fake = FakeGitHubService(bare_repo_path=bare, human_reviewers=["reviewer1"])
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = FakeGitHubServer(fake, bare_repo=bare, head_ref_index={}, scratch=tmp_path,
                             git_port=port)
    for _ in range(2):
        await server.start()
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "ls-remote", server.git_base_url + "/bench-org/bench-repo.git",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            assert proc.returncode == 0, stderr.decode()
            assert b"refs/heads/main" in stdout
        finally:
            await server.stop()
        for _attempt in range(50):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", server.git_port)) != 0:
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("git listener survived benchmark teardown")


def test_live_launcher_uses_a_valid_codex_provider_configuration(monkeypatch):
    import runpy
    import tomllib
    from pathlib import Path

    from performer.backends.codex import _build_provider_config_toml

    monkeypatch.setenv("LITELLM_MASTER_KEY", "test-only-gateway-key")
    factory = runpy.run_path("specs/161-board-bench-harness/live/bench_harness_live.py")["configuration"]
    config = factory("test-performer", Path("specs/161-board-bench-harness/live/routing.yaml"))
    env = config.global_config.performer_endpoints[0].env
    provider = tomllib.loads(_build_provider_config_toml(env))
    custom = provider["model_providers"]["custom"]
    assert custom["base_url"] == env["CODEX_PROVIDER_BASE_URL"]
    # Codex rejects explicit chat wire enums; omitting it is the backend contract.
    assert "wire_api" not in custom
