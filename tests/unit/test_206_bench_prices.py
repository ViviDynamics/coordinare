"""Spec 206: the bench prices runs with the daemon's price table, and unknown cost stays unknown.

Covers: a priced model gets its table entry (exact, cache-aware where the caller has the
split, and input-rate on a bare total); an unpriced model is unknown with the reason; the
ranking and rollups handle a mix of priced and unpriced runs; and score and rollup files
written before this spec still load.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from coordinare.bench import artifact as artifact_module
from coordinare.bench.artifact import LEGACY_REASON, Cost, PersonaDispatch, RunArtifact
from coordinare.bench.fixtures import tiny_fixture
from coordinare.bench.harness_rank import format_report, rank, rank_role
from coordinare.bench.harness_rollup import HarnessRollup, RoleHarnessRow, format_table, roll_up
from coordinare.bench.noise import aggregate
from coordinare.bench.pricing import (
    BASIS_PER_KIND,
    NO_TOKENS_REASON,
    SPLIT_NOT_REPORTED_REASON,
    TokenCost,
    bench_price_table,
    combine,
    price_counts,
    price_table_for_run,
    price_tokens,
)
from coordinare.bench.runner import _build_artifact
from coordinare.bench.score import (
    ComponentVector,
    ScoreObject,
    Weights,
    cost_is_known,
    numeric_component,
)
from coordinare.bench.sweep import PointResult, _dropped_reason, rank_candidates
from coordinare.config import CostTrackingConfig, ModelPriceConfig, ProjectConfiguration
from coordinare.services.fake_github import FakeGitHubService
from coordinare.services.token_usage import UsageCounts, price_table_from_config
from tests.unit.test_161_fixtures import artifact, card, dispatch

OPUS = "claude-opus-4-5"
HAIKU = "claude-haiku-4-5-20251001"
MYSTERY = "mystery-model-9"

_OPUS_PRICE = ModelPriceConfig(
    models=["claude-opus-4-5", "claude-opus-4-5-*"],
    input_per_million=15.0,
    output_per_million=75.0,
    cache_read_per_million=1.5,
    cache_write_per_million=18.75,
)
_HAIKU_PRICE = ModelPriceConfig(
    models=["claude-haiku-4-5*"],
    input_per_million=1.0,
    output_per_million=5.0,
    cache_read_per_million=0.1,
    cache_write_per_million=1.25,
)


_MINIMAL = {
    "project_name": "acme",
    "github_org": "acme",
    "github_project_number": 1,
    "github_token": "ghp_test_token_value",
    "human_reviewers": ["reviewer1"],
}


def _tracking(**extra: object) -> CostTrackingConfig:
    return CostTrackingConfig(
        pricing_as_of=date(2026, 10, 1), prices=[_OPUS_PRICE, _HAIKU_PRICE], **extra,
    )


TABLE = bench_price_table(_tracking())


# --- pricing a dispatch -----------------------------------------------------------------


def test_bare_total_is_unknown_even_for_a_priced_model() -> None:
    """Output and cache tokens cost different amounts, so a total with no split has no honest
    price. It is unknown with the split reason, whatever the model's table entry says."""
    priced = price_tokens(1_000_000, OPUS, TABLE)

    assert priced.cost_usd is None
    assert priced.unknown_reason == SPLIT_NOT_REPORTED_REASON
    assert priced.basis is None


def test_prefix_entry_prices_a_dated_model_id() -> None:
    counts = UsageCounts(input_tokens=1_000_000)
    assert price_counts(counts, HAIKU, TABLE, basis=BASIS_PER_KIND).cost_usd == pytest.approx(1.0)


def test_exact_token_kinds_use_cache_read_and_write_rates() -> None:
    """When the split is known, each kind is priced at its own rate, cache included."""
    counts = UsageCounts(input_tokens=1_000_000, output_tokens=1_000_000,
                         cache_read=1_000_000, cache_write=1_000_000)

    priced = price_counts(counts, OPUS, TABLE, basis=BASIS_PER_KIND)

    # 15 + 75 + 1.5 + 18.75
    assert priced.cost_usd == pytest.approx(110.25)
    assert priced.basis == BASIS_PER_KIND


def test_unpriced_model_is_unknown_with_the_reason_not_a_guess() -> None:
    priced = price_tokens(5_000, MYSTERY, TABLE)

    assert priced.cost_usd is None
    assert priced.unknown_reason is not None
    assert SPLIT_NOT_REPORTED_REASON in priced.unknown_reason
    assert f"no price for {MYSTERY}" in priced.unknown_reason
    assert priced.basis is None


def test_dispatch_without_a_recorded_model_is_unknown() -> None:
    priced = price_tokens(5_000, None, TABLE)

    assert priced.cost_usd is None
    assert priced.unknown_reason is not None
    assert "a model not recorded" in priced.unknown_reason


def test_missing_token_count_is_unknown_and_zero_tokens_is_still_unknown() -> None:
    """Absent is not zero (spec 161, 305), and a zero total has no split either."""
    assert price_tokens(None, OPUS, TABLE) == TokenCost(None, NO_TOKENS_REASON, None)
    assert price_tokens(0, OPUS, TABLE).cost_usd is None


def test_no_price_table_leaves_every_model_unknown() -> None:
    assert price_tokens(1_000_000, OPUS, bench_price_table(None)).cost_usd is None


def test_default_price_covers_unmatched_models_and_not_priced_ones() -> None:
    table = bench_price_table(_tracking(
        default_price={"input_per_million": 2.0, "output_per_million": 2.0,
                       "cache_read_per_million": 0.2, "cache_write_per_million": 2.5},
    ))
    counts = UsageCounts(input_tokens=1_000_000)

    assert price_counts(counts, MYSTERY, table, basis=BASIS_PER_KIND).cost_usd == pytest.approx(2.0)
    assert price_counts(counts, OPUS, table, basis=BASIS_PER_KIND).cost_usd == pytest.approx(15.0)


def test_deprecated_flat_rate_never_reaches_the_bench() -> None:
    """The daemon's deprecated flat rate prices every model at one rate. The bench does not
    read it: an unrecorded or unknown model stays unknown, never flat-priced."""
    flat = CostTrackingConfig(cost_per_million_tokens=3.0)
    counts = UsageCounts(input_tokens=1_000_000)

    assert price_table_from_config(flat).resolve(MYSTERY) is not None  # the daemon would
    assert price_counts(counts, MYSTERY, bench_price_table(flat), basis=BASIS_PER_KIND).cost_usd is None


def test_combine_makes_a_card_unknown_when_any_dispatch_is_unknown() -> None:
    counts = UsageCounts(input_tokens=1_000_000)
    priced = price_counts(counts, OPUS, TABLE, basis=BASIS_PER_KIND)
    unknown = price_tokens(1_000_000, MYSTERY, TABLE)

    mixed = combine([priced, unknown])
    assert mixed.cost_usd is None
    assert mixed.unknown_reason is not None and SPLIT_NOT_REPORTED_REASON in mixed.unknown_reason

    all_priced = combine([priced, price_counts(counts, HAIKU, TABLE, basis=BASIS_PER_KIND)])
    assert all_priced.cost_usd == pytest.approx(16.0)
    assert all_priced.basis == BASIS_PER_KIND

    assert combine([]).unknown_reason == NO_TOKENS_REASON


# --- the config loader is the daemon's ---------------------------------------------------


def test_bench_reads_the_daemon_config_through_the_same_loader() -> None:
    cfg = ProjectConfiguration(**_MINIMAL, cost_tracking=_tracking())

    table = price_table_for_run(real_config=cfg)

    counts = UsageCounts(input_tokens=1_000_000)
    assert price_counts(counts, OPUS, table, basis=BASIS_PER_KIND).cost_usd == pytest.approx(15.0)


def test_bench_loads_cost_tracking_from_a_config_file(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "project_name: acme\n"
        "github_org: acme\n"
        "github_project_number: 1\n"
        "github_token: ghp_test_token_value\n"
        "human_reviewers: [reviewer1]\n"
        "cost_tracking:\n"
        "  pricing_as_of: '2026-10-01'\n"
        "  prices:\n"
        "    - models: ['claude-opus-4-5*']\n"
        "      input_per_million: 15.0\n"
        "      output_per_million: 75.0\n"
        "      cache_read_per_million: 1.5\n"
        "      cache_write_per_million: 18.75\n",
    )

    table = price_table_for_run(config_path=config)

    counts = UsageCounts(input_tokens=1_000_000)
    assert price_counts(counts, OPUS, table, basis=BASIS_PER_KIND).cost_usd == pytest.approx(15.0)
    assert price_counts(counts, MYSTERY, table, basis=BASIS_PER_KIND).cost_usd is None


def test_no_config_at_all_is_an_empty_table(tmp_path: Path) -> None:
    table = price_table_for_run(config_path=tmp_path / "missing.yaml")

    assert price_tokens(1_000_000, OPUS, table).cost_usd is None


# --- ranking with a mix of priced and unpriced runs --------------------------------------


def _priced_row(backend: str, credit: int, cost: float | None, *, reasons: list[str] | None = None,
                partial: bool = False) -> RoleHarnessRow:
    return RoleHarnessRow(
        role="reviewer", backend=backend, dispatches=4, credit=credit,
        cost_usd=cost, cost_partial=partial, cost_unknown_reasons=reasons or [],
        runs_contributing=1, per_run_scalars=[credit / 4],
    )


def test_ranking_orders_priced_pairs_and_excludes_unknown_cost_with_its_reason() -> None:
    rows = [
        _priced_row("costly", 4, 0.5),
        _priced_row("unknown", 4, None, reasons=[f"no price for {MYSTERY}"]),
        _priced_row("cheaper", 4, 0.1),
    ]

    ranking = rank_role("reviewer", rows, Weights())

    assert [s.backend for s in ranking.ordered] == ["cheaper", "costly"]
    assert ranking.cost_unknown == {"unknown": f"no price for {MYSTERY}"}
    assert "unknown" not in [s.backend for s in ranking.ordered]
    assert "excluded, cost unknown" in format_report(rank(_report_for(rows)))


def test_partial_cost_is_excluded_from_a_cost_weighted_ranking() -> None:
    rows = [_priced_row("whole", 4, 0.5), _priced_row("partial", 4, 0.2, partial=True,
                                                      reasons=["no price for x"])]

    ranking = rank_role("reviewer", rows, Weights())

    assert [s.backend for s in ranking.ordered] == ["whole"]
    assert "partial" in ranking.cost_unknown


def test_unknown_cost_ranks_when_the_ranking_does_not_weight_cost() -> None:
    rows = [_priced_row("cheap", 4, 0.5), _priced_row("unknown", 4, None, reasons=["no price"])]

    ranking = rank_role("reviewer", rows, Weights(w_cost=0.0))

    assert {s.backend for s in ranking.ordered} == {"cheap", "unknown"}
    assert ranking.cost_unknown == {}


def _report_for(rows: list[RoleHarnessRow]) -> HarnessRollup:
    return HarnessRollup(run_ids=["r"], rows=rows, generated_at=datetime.now(UTC),
                         artifact_schema_versions=[1])


# --- rollups say partial ----------------------------------------------------------------


def _dispatch_with_cost(session: str, cost: float | None, *, basis: str | None,
                        reason: str | None = None) -> PersonaDispatch:
    d = dispatch("reviewer", "claude_code", "approved", session_id=session, tokens=1_000)
    d.model = OPUS
    d.cost_usd = cost
    d.cost_basis = basis
    d.cost_unknown_reason = reason
    return d


def test_rollup_sums_priced_dispatches_and_marks_the_sum_partial() -> None:
    run = artifact([card("c1", [
        _dispatch_with_cost("s1", 2.0, basis=BASIS_PER_KIND),
        _dispatch_with_cost("s2", None, basis=None, reason=f"no price for {MYSTERY}"),
    ])])

    row = roll_up([run]).rows[0]

    assert row.cost_usd == pytest.approx(2.0)
    assert row.cost_partial is True
    assert row.cost_unknown_reasons == [f"no price for {MYSTERY}"]
    assert "partial" in format_table(roll_up([run]))


def test_rollup_of_an_all_priced_pair_is_not_partial() -> None:
    run = artifact([card("c1", [_dispatch_with_cost("s1", 2.0, basis=BASIS_PER_KIND)])])

    row = roll_up([run]).rows[0]

    assert row.cost_usd == pytest.approx(2.0)
    assert row.cost_partial is False


def test_legacy_dispatch_with_no_price_reports_the_legacy_reason() -> None:
    """A run recorded before spec 206 has no per-dispatch price. It is unknown, and says why."""
    run = artifact([card("c1", [dispatch("reviewer", "claude_code", "approved", session_id="s1")])])

    row = roll_up([run]).rows[0]

    assert row.cost_usd is None
    assert row.cost_unknown_reasons == [LEGACY_REASON]


def test_run_totals_are_partial_when_a_card_is_unpriced() -> None:
    priced = Cost(tokens_processed=1, cost_usd=1.5, cost_basis=BASIS_PER_KIND)
    unknown = Cost(tokens_processed=1, cost_unknown_reason=f"no price for {MYSTERY}")
    run = artifact([card("c1", []), card("c2", [])])
    run.cards[0].cost = priced
    run.cards[1].cost = unknown

    totals = run.compute_totals()

    assert totals.cost_usd == pytest.approx(1.5)
    assert totals.cost_partial is True
    assert totals.cost_unpriced_cards == 1
    assert totals.cost_unknown_reason == f"no price for {MYSTERY}"


def test_run_totals_are_not_partial_when_every_card_is_priced() -> None:
    run = artifact([card("c1", []), card("c2", [])])
    run.cards[0].cost = Cost(cost_usd=1.0, cost_basis=BASIS_PER_KIND)
    run.cards[1].cost = Cost(cost_usd=2.0, cost_basis=BASIS_PER_KIND)

    totals = run.compute_totals()

    assert totals.cost_usd == pytest.approx(3.0)
    assert totals.cost_partial is False
    assert totals.cost_unpriced_cards == 0


# --- sweep and noise never rank a partial or unknown cost as a number --------------------


def test_sweep_point_with_unknown_cost_ranks_last_with_the_reason() -> None:
    priced = PointResult(point_id="priced", kind="candidate", mean_scalar=0.9)
    unknown = PointResult(point_id="unknown", kind="candidate", mean_scalar=None,
                          cost_unpriced_repeats=1, cost_unknown_reason=f"no price for {MYSTERY}")

    assert rank_candidates([unknown, priced]) == ["priced", "unknown"]
    assert _dropped_reason(unknown) == f"unpriced: no price for {MYSTERY}"


def test_unknown_or_partial_cost_is_never_a_number_in_a_component_mean() -> None:
    assert numeric_component(ComponentVector(cost_usd=None), "cost_usd") is None
    assert numeric_component(ComponentVector(cost_usd=2.0, cost_partial=True), "cost_usd") is None
    assert numeric_component(ComponentVector(cost_usd=2.0), "cost_usd") == pytest.approx(2.0)
    assert cost_is_known(ComponentVector(cost_usd=2.0)) is True


def test_noise_report_counts_the_unpriced_repeats() -> None:
    now = datetime(2026, 10, 14, tzinfo=UTC)

    def _score(cost: float | None) -> ScoreObject:
        return ScoreObject(
            run_id="r", artifact_schema_version=1, scored_at=now,
            components=ComponentVector(correctness_rate=1.0, cost_usd=cost,
                                       wall_clock_seconds=60.0),
            scalar=0.9 if cost is not None else None,
        )

    report = aggregate([("h", _score(0.5), "1"), ("h", _score(None), "2")],
                       requested_repeats=2, failures=[])

    assert report.cost_unpriced_repeats == 1
    assert report.component_stats["cost_usd"].n == 1
    assert "cost unpriced in 1 repeat" in report.render_markdown()


def test_flat_estimator_is_gone() -> None:
    """The retired flat-rate helper must not come back as a second pricing path."""
    assert not hasattr(artifact_module, "estimate_cost_usd")


# --- compatibility: files written before spec 206 still load -----------------------------


def test_old_score_file_with_a_numeric_cost_still_loads_and_keeps_its_scalar() -> None:
    legacy = (
        '{"schema_version": 2, "run_id": "old", "artifact_schema_version": 1, '
        '"scored_at": "2026-01-01T00:00:00Z", "scalar": 0.7, '
        '"components": {"correctness_rate": 0.8, "wall_clock_seconds": 300.0, '
        '"cost_usd": 0.5}}'
    )

    loaded = ScoreObject.model_validate_json(legacy)

    assert loaded.scalar == pytest.approx(0.7)
    assert loaded.components.cost_usd == pytest.approx(0.5)
    assert loaded.components.cost_partial is False  # additive default
    assert loaded.components.cost_unknown_reason is None


def test_new_score_fields_round_trip() -> None:
    now = datetime(2026, 10, 14, tzinfo=UTC)
    score = ScoreObject(
        run_id="r", artifact_schema_version=1, scored_at=now,
        components=ComponentVector(correctness_rate=1.0, cost_usd=1.0, cost_partial=True,
                                   cost_unknown_reason=f"no price for {MYSTERY}",
                                   wall_clock_seconds=60.0),
    )

    reloaded = ScoreObject.model_validate_json(score.to_validated_json())

    assert reloaded.components.cost_partial is True
    assert reloaded.components.cost_unknown_reason == f"no price for {MYSTERY}"


def test_old_run_file_with_a_numeric_card_cost_is_not_read_as_a_price() -> None:
    """A card cost with no basis was the retired flat rate. Totals must not pass it off as a
    priced figure."""
    raw = artifact([card("c1", [])]).model_dump(mode="json")
    raw["cards"][0]["cost"] = {"tokens_processed": 1000, "cost_usd": 3.0, "cost_estimated": True}

    run = RunArtifact.model_validate(raw)
    totals = run.compute_totals()

    assert run.cards[0].cost.cost_usd == pytest.approx(3.0)  # loads unchanged
    assert totals.cost_usd is None
    assert totals.cost_partial is True
    assert totals.cost_unknown_reason == LEGACY_REASON


def test_old_run_json_round_trips_without_the_new_fields(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    path.write_text(json.dumps({
        "schema_version": 1, "run_id": "r", "started_at": "2026-10-14T00:00:00Z",
        "finished_at": "2026-10-14T00:00:00Z", "wall_clock_seconds": 1.0,
        "config_fingerprint": {"hash": "h", "source_path": "p"},
    }))

    loaded = RunArtifact.load(path)

    assert loaded.totals.cost_partial is False
    assert loaded.totals.cost_unknown_reason is None


# --- the runner prices each dispatch with its own model ----------------------------------


def _log_row(model: str, tokens: int | None) -> dict[str, Any]:
    now = datetime(2026, 10, 14, tzinfo=UTC)
    return {
        "card_id": "card-1", "stage": "implementing", "role": "implementer",
        "status": "succeeded", "terminal_marker": "pr_opened", "model": model,
        "backend": "claude_code", "job_id": None, "session_id": None, "container_id": None,
        "tokens_processed": tokens, "started_at": now, "finished_at": now, "seconds": 1.0,
        "poll_error": None,
    }


def _built(rows: list[dict[str, Any]], tmp_path: Path) -> RunArtifact:
    now = datetime(2026, 10, 14, tzinfo=UTC)
    return _build_artifact(
        fake=FakeGitHubService(bare_repo_path=tmp_path / "bare.git"),
        fixtures_by_card={"card-1": tiny_fixture()},
        board={},
        dispatch_log=rows,
        started_at=now,
        finished_at=now,
        config_path=None,
        price_table=TABLE,
        run_error=None,
    )


def test_runner_leaves_a_bare_total_dispatch_unknown_with_the_split_reason(tmp_path: Path) -> None:
    """The runner has only a total per dispatch, so no dispatch can price (spec 206)."""
    run = _built([_log_row(OPUS, 1_000_000), _log_row(HAIKU, 1_000_000)], tmp_path)

    dispatches = run.cards[0].dispatches
    assert [d.cost_usd for d in dispatches] == [None, None]
    assert all(d.cost_unknown_reason == SPLIT_NOT_REPORTED_REASON for d in dispatches)
    assert run.cards[0].cost.cost_usd is None
    assert run.totals.cost_partial is True
    assert run.totals.cost_usd is None


def test_runner_names_the_unpriced_model_alongside_the_split(tmp_path: Path) -> None:
    run = _built([_log_row(OPUS, 1_000_000), _log_row(MYSTERY, 1_000)], tmp_path)

    card_cost = run.cards[0].cost
    assert card_cost.cost_usd is None
    assert card_cost.cost_unknown_reason is not None
    assert f"no price for {MYSTERY}" in card_cost.cost_unknown_reason
    assert SPLIT_NOT_REPORTED_REASON in card_cost.cost_unknown_reason


def test_runner_reports_a_dispatch_with_no_token_count_as_unknown_not_zero(tmp_path: Path) -> None:
    run = _built([_log_row(OPUS, None)], tmp_path)

    assert run.cards[0].dispatches[0].cost_usd is None
    assert run.cards[0].cost.cost_usd is None
    assert run.cards[0].cost.cost_unknown_reason == NO_TOKENS_REASON

