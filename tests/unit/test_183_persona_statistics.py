from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.config import ProjectConfiguration, SymphonyConfig
from coordinare.services.run_history import HistoryFilter, HistoryQuery, UsageSource, list_history
from coordinare.services.run_statistics import RunStatistics
from coordinare.services.run_transcript import transcript_path
from coordinare.services.stats_params import StatsQuery
from tests.unit.test_182_run_statistics import NOW, TABLE, VISIBLE, client, write_run


def version_run(root: Path, run: str, *, symphony: str = "alpha", hours: int = 1) -> None:
    write_run(root, run, run, persona="reviewer", symphony=symphony, hours=hours)
    path = transcript_path(root, symphony=symphony, card_id=run, run_id=run)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[0]["persona"].update(version=2, version_sha256="abc", changed_at=records[0]["ts"])
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


@pytest.mark.asyncio
async def test_recent_version_and_cost_measures_share_filtered_cohort(tmp_path: Path) -> None:
    for index in range(12):
        version_run(tmp_path, f"r{index:02}", hours=index + 1)
    version_run(tmp_path, "other", symphony="beta")
    write_run(tmp_path, "old", "old", hours=200, persona="reviewer")
    write_run(tmp_path, "implementer", "i")
    service = RunStatistics(tmp_path, table=TABLE)
    body = await service.query(
        StatsQuery(agent="reviewer", symphony="alpha", group_by="symphony", window="24h"),
        visible=VISIBLE, lifecycle={}, boards={}, now=NOW,
    )
    assert body["totals"]["runs"] == 12
    assert body["totals"]["cost_per_run_usd"] == pytest.approx(body["totals"]["cost"]["usd"] / 12)
    assert len(body["recent_runs"]) == 10
    assert [row["run"] for row in body["recent_runs"]] == [f"r{index:02}" for index in range(10)]
    row = body["recent_runs"][0]
    assert row["symphony"] == "alpha"
    assert row["agent"] == "reviewer"
    assert row["duration_seconds"] == 60
    assert row["cost"]["usd"] == body["totals"]["cost_per_run_usd"]
    assert body["persona_versions"][0]["runs"] == 12
    assert body["persona_versions"][0]["version"] == 2
    assert body["persona_versions"][0]["sha256"] == "abc"
    assert body["persona_versions"][0]["symphony"] == "alpha"
    assert body["daily"][0]["persona_edits"][0]["version"] == 2


@pytest.mark.asyncio
async def test_missing_usage_and_legacy_versions_stay_unknown(tmp_path: Path) -> None:
    write_run(tmp_path, "legacy", "card", persona="reviewer")
    path = transcript_path(tmp_path, symphony="alpha", card_id="card", run_id="legacy")
    records = [json.loads(line) for line in path.read_text().splitlines()]
    path.write_text("".join(json.dumps(row) + "\n" for row in records if row["type"] != "usage"))
    body = await RunStatistics(tmp_path).query(
        StatsQuery(agent="reviewer"), visible=VISIBLE, lifecycle={}, boards={}, now=NOW,
    )
    assert body["totals"]["missing_usage_runs"] == 1
    assert body["totals"]["cost_per_run_usd"] is None
    assert body["recent_runs"][0]["cost"]["usd"] is None
    assert body["persona_versions"][0]["version"] is None
    assert body["daily"][0]["persona_edits"] == []


def test_history_agent_filter_is_persona_and_part_of_cursor_identity(tmp_path: Path) -> None:
    write_run(tmp_path, "review", "card", persona="reviewer")
    write_run(tmp_path, "implement", "card")
    body = list_history(
        tmp_path, HistoryQuery(filters=HistoryFilter(agent="reviewer"), visible=VISIBLE),
        UsageSource(ledger=None, table=TABLE),
    )
    assert [row["run"] for row in body["rows"]] == ["review"]


def test_runtime_resolution_overrides_scope_and_active_runs() -> None:
    config = ProjectConfiguration.model_validate({
        "github_token": "test", "github_org": "example", "human_reviewers": ["operator"],
        "performers": {"reviewer": {"backend": "codex", "host": "worker", "mode": "fast"}},
        "endpoints": [{"name": "cloud", "kind": "openai"}],
        "model_endpoints": [{"name": "tool", "model": "model-a", "endpoint": "cloud"}],
        "modes": [{"name": "fast", "strategy": "single", "tool": "tool"}],
    })
    alpha = SymphonyConfig(name="alpha", github_project_number=1, personas={"reviewer": {"instructions": "custom"}})
    session = {"performer_stage": "reviewing", "phase": "monitoring_performer",
               "agent_dispatch": {"session_id": "live"}, "current_card": {"id": "card", "title": "Working"}}
    from_state = {"config": config, "symphony_configs": {"alpha": alpha},
                  "symphony_states": {"alpha": SimpleNamespace(active_sessions={"card": session})}}
    result = RunStatistics.persona_runtime(from_state, "reviewer", frozenset({"alpha"}))
    assert result["stage"] == "reviewing"
    resolved = result["symphonies"][0]
    assert resolved["persona_override"] is True
    assert resolved["model"] == "model-a"
    assert resolved["mode"] == "fast"
    assert resolved["hosts"] == ["worker"]
    assert result["activity"][0]["run"] == "live"
    beta = SymphonyConfig(name="beta", github_project_number=2, overrides={"performers": {
        "reviewer": {"backend": "codex", "host": "https://user:private@beta-worker/path?token=secret", "mode": "fast"},
    }})
    from_state["symphony_configs"]["beta"] = beta
    beta_result = RunStatistics.persona_runtime(from_state, "reviewer", frozenset({"beta"}))
    assert beta_result["symphonies"][0]["hosts"] == ["beta-worker"]
    assert beta_result["symphonies"][0]["persona_override"] is False
    assert beta_result["activity"] == []
    assert "private" not in json.dumps(beta_result)
    assert "secret" not in json.dumps(beta_result)
    assert RunStatistics.persona_runtime(from_state, "reviewer", frozenset())["activity"] == []


def test_persona_panel_under_node() -> None:
    result = subprocess.run(
        ["node", "tests/js/persona_stats_checks.js"], capture_output=True, text=True,
        check=False, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_version_identity_keeps_content_and_symphonies_separate(tmp_path: Path) -> None:
    version_run(tmp_path, "a")
    version_run(tmp_path, "b", symphony="beta")
    body = await RunStatistics(tmp_path, table=TABLE).query(
        StatsQuery(agent="reviewer"), visible=VISIBLE, lifecycle={}, boards={}, now=NOW,
    )
    assert {row["symphony"] for row in body["persona_versions"]} == VISIBLE
    assert sum(row["runs"] for row in body["persona_versions"]) == body["totals"]["runs"]
    assert len(body["persona_edits"]) == 2


@pytest.mark.parametrize("window,runs", [("24h", 1), ("7d", 2), ("30d", 3)])
def test_stats_api_figures_render_without_reaggregation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window: str, runs: int,
) -> None:
    write_run(tmp_path, "today", "today", persona="reviewer")
    write_run(tmp_path, "week", "week", persona="reviewer", hours=48, duration=120)
    write_run(tmp_path, "month", "month", persona="reviewer", hours=240, duration=180)
    original = RunStatistics.query

    async def fixed_query(self: RunStatistics, params: StatsQuery, **kwargs: Any) -> dict[str, Any]:
        kwargs["now"] = NOW
        kwargs["lifecycle"] = {"alpha": {
            run: [{"kind": "verdict", "stage": "reviewing", "at": (NOW - timedelta(hours=hours)).isoformat(),
                   "detail": {"run_id": run, "result": verdict}}]
            for run, hours, verdict in [("today", 1, "pass"), ("week", 48, "fail"), ("month", 240, "pass")]
        }}
        return await original(self, params, **kwargs)

    monkeypatch.setattr(RunStatistics, "query", fixed_query)
    monkeypatch.setattr("coordinare.dashboard.routers.stats.price_table_from_config", lambda _config: TABLE)
    with client(tmp_path) as api:
        response = api.get(f"/api/stats?agent=reviewer&symphony=alpha&group_by=symphony&window={window}")
    assert response.status_code == 200
    body = response.json()
    assert body["totals"]["runs"] == runs
    assert body["totals"]["tokens"]["total"] == runs * 165
    assert body["groups"][0]["runs"] == runs
    assert body["totals"]["success_rate"] == pytest.approx(1 if runs == 1 else (runs - 1) / runs)
    assert body["runtime"]["stage"] == "reviewing"
    fixture = tmp_path / "stats.json"
    fixture.write_text(json.dumps(body))
    result = subprocess.run(
        ["node", "tests/js/persona_stats_checks.js", str(fixture)],
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_shared_edit_after_latest_run_is_visible_without_inventing_run_version(
    tmp_path: Path,
) -> None:
    write_run(tmp_path, "before-edit", "card", persona="reviewer", hours=2)
    root = tmp_path / "persona-history"
    root.mkdir()
    (root / "reviewer.jsonl").write_text(json.dumps({
        "role": "reviewer", "version": 3, "sha256": "new-content", "text": "Private instruction text",
        "changed_at": NOW.isoformat(), "changed_by": "operator", "source": "dashboard",
    }) + "\n")
    body = await RunStatistics(tmp_path, persona_root=root).query(
        StatsQuery(agent="reviewer", symphony="alpha"), visible=VISIBLE, lifecycle={}, boards={}, now=NOW,
    )
    assert body["shared_persona_edits"][0]["version"] == 3
    assert body["shared_persona_edits"][0]["scope"] == "shared_history"
    assert "Private instruction text" not in json.dumps(body)
    assert body["persona_versions"][0]["version"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_at", ["2026-10-15T11:00:00+00:00", "2026-10-15T13:00:00+02:00"])
async def test_shared_edits_exclude_attributed_version_and_timestamp(
    tmp_path: Path, changed_at: str,
) -> None:
    version_run(tmp_path, "a")
    version_run(tmp_path, "b", symphony="beta")
    root = tmp_path / "persona-history"
    root.mkdir()
    edits = [
        {"version": 2, "changed_at": changed_at},
        {"version": 3, "changed_at": changed_at},
        {"version": 2, "changed_at": NOW.isoformat()},
    ]
    (root / "reviewer.jsonl").write_text("".join(json.dumps({
        "role": "reviewer", "sha256": "abc", "text": "private", "changed_by": "operator",
        "source": "dashboard", **edit,
    }) + "\n" for edit in edits))
    body = await RunStatistics(tmp_path, persona_root=root).query(
        StatsQuery(agent="reviewer"), visible=VISIBLE, lifecycle={}, boards={}, now=NOW,
    )
    assert len(body["persona_edits"]) == 2
    assert {edit["symphony"] for edit in body["persona_edits"]} == VISIBLE
    assert [(edit["version"], edit["changed_at"]) for edit in body["shared_persona_edits"]] == [
        (3, changed_at), (2, NOW.isoformat()),
    ]


@pytest.mark.asyncio
async def test_edit_buckets_use_utc_day_and_keep_edits_without_runs_that_day(tmp_path: Path) -> None:
    for run, hours, changed_at in [
        ("today", 1, "2026-10-15T01:00:00+02:00"),
        ("yesterday", 24, "2026-10-15T01:00:00+02:00"),
        ("earlier", 48, "2026-10-12T23:00:00+00:00"),
    ]:
        version_run(tmp_path, run, hours=hours)
        path = transcript_path(tmp_path, symphony="alpha", card_id=run, run_id=run)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        records[0]["persona"]["changed_at"] = changed_at
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
    body = await RunStatistics(tmp_path).query(
        StatsQuery(agent="reviewer"), visible=VISIBLE, lifecycle={}, boards={}, now=NOW,
    )
    assert [edit["changed_at"] for edit in body["persona_edits"]] == [
        "2026-10-12T23:00:00+00:00", "2026-10-14T23:00:00+00:00",
    ]
    days = {day["day"]: day["persona_edits"] for day in body["daily"]}
    assert days["2026-10-14"] == [body["persona_edits"][1]]
    assert days["2026-10-15"] == []


def test_history_agent_length_limit(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        assert api.get("/api/history/runs", params={"agent": "a" * 1024}).status_code == 200
        assert api.get("/api/history/runs", params={"agent": "a" * 1025}).status_code == 422


@pytest.mark.parametrize("check", ["zero", "scale", "coordinates", "definitions", "refresh"])
def test_review_regressions_under_node(check: str) -> None:
    result = subprocess.run(
        ["node", "tests/js/stats_review_checks.js", check], capture_output=True, text=True,
        check=False, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
