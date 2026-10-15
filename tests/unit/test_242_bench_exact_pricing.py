"""Spec 242: the bench prices a real run exactly from its transcript usage, per kind.

Covers: a run with ledger usage prices exactly, each kind at its own rate; a run with no
transcript stays unknown and says why; a mixed sweep ranks the priced points and leaves the
unknown one last with its reason; a default-weight sweep ranks by cost; a stub run reports
zero tokens of every kind and prices at zero; and the ledger that feeds the price is the
daemon's (spec 134), keyed by each dispatch's session id.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.bench.artifact import RunArtifact
from coordinare.bench.fixtures import tiny_fixture
from coordinare.bench.pricing import (
    BASIS_NO_MODEL_CALLS,
    BASIS_PER_KIND,
    SPLIT_NOT_REPORTED_REASON,
    bench_price_table,
    combine,
    price_dispatch,
    price_usage,
)
from coordinare.bench.runner import (
    _attach_transcript_usage,
    _build_artifact,
    _run_transcripts,
    _RunTranscripts,
)
from coordinare.bench.score import ComponentVector, Weights, compute_scalar
from coordinare.bench.sweep import PointResult, rank_candidates
from coordinare.config import CostTrackingConfig, ModelPriceConfig
from coordinare.services.run_transcript import RootGuard, make_collector
from coordinare.services.token_usage import UsageCounts

OPUS = "claude-opus-4-5"
HAIKU = "claude-haiku-4-5-20251001"
MYSTERY = "mystery-model-9"
_MILLION = 1_000_000
_MAX_BYTES = 1 << 20

TABLE = bench_price_table(CostTrackingConfig(
    pricing_as_of=date(2026, 10, 1),
    prices=[
        ModelPriceConfig(
            models=["claude-opus-4-5"], input_per_million=15.0, output_per_million=75.0,
            cache_read_per_million=1.5, cache_write_per_million=18.75,
        ),
        ModelPriceConfig(
            models=["claude-haiku-4-5*"], input_per_million=1.0, output_per_million=5.0,
            cache_read_per_million=0.1, cache_write_per_million=1.25,
        ),
    ],
))

_NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def _usage_record(seq: int, model: str, **kinds: int) -> dict[str, Any]:
    """A transcript ``usage`` line as the performer writes it (message scope, one model turn)."""
    return {
        "type": "usage", "seq": seq, "ts": "2026-10-14T10:00:00+00:00", "scope": "message",
        "usage": {"model": model, **kinds},
    }


def _dispatch_log(session_id: str | None, *, card_id: str = "PVTI_1", model: str = OPUS,
                  tokens: int | None = 1_000) -> dict[str, Any]:
    return {
        "stage": "implementing", "role": "implementer", "card_id": card_id, "model": model,
        "backend": "claude_code", "status": "succeeded", "terminal_marker": "pr_opened",
        "job_id": "job", "session_id": session_id, "container_id": None,
        "tokens_processed": tokens, "started_at": _NOW, "finished_at": _NOW, "seconds": 1.0,
        "poll_error": None,
    }


def _build(fixture_log: list[dict[str, Any]]) -> RunArtifact:
    """A real artifact built from dispatch-log rows, with an empty fake GitHub (no PRs, no CI)."""
    fx = tiny_fixture()
    fake: Any = SimpleNamespace(_prs={}, events=[], _cards={}, _human_reviewers=[])
    return _build_artifact(
        fake=fake,
        fixtures_by_card={"PVTI_1": fx},
        board={"snapshot": {}},
        dispatch_log=fixture_log,
        started_at=_NOW,
        finished_at=_NOW,
        config_path=None,
        price_table=TABLE,
        run_error=None,
        config=None,
    )


# --- a run with ledger usage prices exactly, each kind at its own rate -------------------


def test_usage_prices_each_kind_at_its_own_rate() -> None:
    """1M input at 15, 100k output at 75, 2M cache read at 1.5: 15 + 7.5 + 3.0 = 25.5."""
    usage = {OPUS: UsageCounts(input_tokens=_MILLION, output_tokens=100_000, cache_read=2 * _MILLION)}

    priced = price_usage(usage, TABLE, basis=BASIS_PER_KIND)

    assert priced.cost_usd == pytest.approx(25.5)
    assert priced.unknown_reason is None
    assert priced.basis == BASIS_PER_KIND


def test_usage_across_two_models_sums_each_at_its_own_price() -> None:
    usage = {
        OPUS: UsageCounts(input_tokens=_MILLION),
        HAIKU: UsageCounts(output_tokens=_MILLION),
    }

    assert price_usage(usage, TABLE, basis=BASIS_PER_KIND).cost_usd == pytest.approx(20.0)


def test_a_dispatch_with_ledger_usage_is_priced_exactly_in_the_artifact() -> None:
    usage = {OPUS: UsageCounts(input_tokens=_MILLION, output_tokens=100_000, cache_read=2 * _MILLION)}
    row = _dispatch_log("sess-1")
    row["usage"] = usage

    artifact = _build([row])

    dispatch = artifact.cards[0].dispatches[0]
    assert dispatch.cost_usd == pytest.approx(25.5)
    assert dispatch.cost_basis == BASIS_PER_KIND
    assert dispatch.cost_unknown_reason is None
    assert artifact.cards[0].cost.cost_usd == pytest.approx(25.5)
    assert artifact.totals.cost_usd == pytest.approx(25.5)
    assert artifact.totals.cost_priced is True
    assert artifact.totals.cost_partial is False


def test_the_ledger_is_the_source_and_is_keyed_by_session(tmp_path: Path) -> None:
    """The usage a dispatch is priced from is the daemon ledger's, read by its session id."""
    collector = make_collector(
        enabled=True, root=tmp_path, max_bytes_per_run=_MAX_BYTES, retention_days=30, guard=RootGuard(),
    )
    collector.usage.observe(
        "sess-1", symphony="acme", card="PVTI_1", stage="implementing",
        records=[_usage_record(1, OPUS, input_tokens=_MILLION, output_tokens=100_000, cache_read=2 * _MILLION)],
    )
    log = [_dispatch_log("sess-1"), _dispatch_log("sess-unrecorded")]

    _attach_transcript_usage(log, _RunTranscripts(collector=collector, disabled_reason=None))

    assert log[0]["usage"] == {OPUS: UsageCounts(input_tokens=_MILLION, output_tokens=100_000,
                                                 cache_read=2 * _MILLION)}
    assert log[1]["usage"] is None
    artifact = _build(log)
    assert artifact.cards[0].dispatches[0].cost_usd == pytest.approx(25.5)


# --- a run with no transcript stays unknown, with its reason -----------------------------


def test_a_dispatch_with_no_transcript_usage_is_unknown_with_its_reason() -> None:
    artifact = _build([_dispatch_log("sess-missing", tokens=5_000)])

    dispatch = artifact.cards[0].dispatches[0]
    assert dispatch.cost_usd is None
    assert dispatch.cost_basis is None
    assert dispatch.cost_unknown_reason == SPLIT_NOT_REPORTED_REASON
    assert artifact.totals.cost_usd is None
    assert artifact.totals.cost_unknown_reason is not None


def test_an_unpriced_model_in_the_usage_is_unknown_and_named() -> None:
    usage = {MYSTERY: UsageCounts(input_tokens=5_000)}

    priced = price_dispatch(usage, None, None, TABLE)

    assert priced.cost_usd is None
    assert priced.unknown_reason is not None
    assert f"no price for {MYSTERY}" in priced.unknown_reason


def test_a_usage_record_with_no_model_is_unknown() -> None:
    usage = {None: UsageCounts(input_tokens=5_000)}

    priced = price_usage(usage, TABLE, basis=BASIS_PER_KIND)

    assert priced.cost_usd is None
    assert priced.unknown_reason is not None
    assert "a model not recorded" in priced.unknown_reason


# --- stub: zero tokens of every kind, priced at zero -------------------------------------


def test_a_stub_dispatch_with_no_model_calls_prices_at_zero_not_unknown() -> None:
    priced = price_dispatch({}, 0, None, TABLE)

    assert priced.cost_usd == 0.0
    assert priced.unknown_reason is None
    assert priced.basis == BASIS_NO_MODEL_CALLS


def test_a_stub_run_prices_every_card_at_zero_and_is_ranked_by_cost() -> None:
    stub_row = _dispatch_log(None, model="", tokens=0)
    stub_row["usage"] = {}

    artifact = _build([stub_row])

    assert artifact.cards[0].cost.cost_usd == 0.0
    assert artifact.cards[0].cost.cost_basis == BASIS_NO_MODEL_CALLS
    assert artifact.totals.cost_usd == 0.0
    assert artifact.totals.cost_partial is False


# --- mixed sweep and default-weight ranking ----------------------------------------------


def _scalar(artifact: RunArtifact, correctness: float = 1.0) -> float | None:
    components = ComponentVector(
        correctness_rate=correctness,
        cost_usd=artifact.totals.cost_usd,
        cost_priced=artifact.totals.cost_priced,
        cost_partial=artifact.totals.cost_partial,
        cost_unknown_reason=artifact.totals.cost_unknown_reason,
        wall_clock_seconds=artifact.wall_clock_seconds,
    )
    scalar, _missing = compute_scalar(components, Weights())
    return scalar


def _point(point_id: str, scalar: float | None) -> PointResult:
    return PointResult(point_id=point_id, kind="candidate", mean_scalar=scalar)


def test_default_weight_sweep_ranks_the_cheaper_priced_run_first() -> None:
    cheap = _build([_with_usage(_dispatch_log("cheap"), {HAIKU: UsageCounts(input_tokens=_MILLION)})])
    costly = _build([_with_usage(_dispatch_log("costly"), {OPUS: UsageCounts(input_tokens=_MILLION)})])
    cheap_scalar = _scalar(cheap)
    costly_scalar = _scalar(costly)

    assert Weights().w_cost == pytest.approx(0.1)
    assert cheap_scalar is not None
    assert costly_scalar is not None
    assert cheap_scalar > costly_scalar
    order = rank_candidates([_point("costly", costly_scalar), _point("cheap", cheap_scalar)])
    assert order == ["cheap", "costly"]


def test_mixed_sweep_ranks_priced_points_and_leaves_the_unknown_last_with_its_reason() -> None:
    priced = _build([_with_usage(_dispatch_log("priced"), {OPUS: UsageCounts(input_tokens=_MILLION)})])
    unknown = _build([_dispatch_log("no-transcript", tokens=5_000)])

    assert _scalar(priced) is not None
    assert _scalar(unknown) is None
    assert unknown.totals.cost_unknown_reason is not None
    points = [
        _point("unknown", _scalar(unknown)),
        _point("priced", _scalar(priced)),
    ]
    assert rank_candidates(points) == ["priced", "unknown"]


def _with_usage(row: dict[str, Any], usage: dict[str | None, UsageCounts]) -> dict[str, Any]:
    row["usage"] = usage
    return row


def test_dispatch_model_field_is_not_used_to_price_an_exact_dispatch() -> None:
    """The price comes from the usage record's model, not the dispatch's configured model."""
    row = _dispatch_log("sess", model=HAIKU)
    row["usage"] = {OPUS: UsageCounts(input_tokens=_MILLION)}

    artifact = _build([row])

    assert artifact.cards[0].dispatches[0].cost_usd == pytest.approx(15.0)


def test_combine_of_exact_and_zero_stub_dispatches_is_exact() -> None:
    exact = price_usage({OPUS: UsageCounts(input_tokens=_MILLION)}, TABLE, basis=BASIS_PER_KIND)
    zero = price_dispatch({}, 0, None, TABLE)

    total = combine([exact, zero])

    assert total.cost_usd == pytest.approx(15.0)
    assert total.unknown_reason is None


# --- the wiring: a stub run end to end, and where a real run keeps its transcripts -------


async def test_a_stub_run_board_prices_every_dispatch_at_zero(tmp_path: Path) -> None:
    from coordinare.bench.runner import run_board

    artifact = await run_board([tiny_fixture()], tmp_path / "run", stub=True)

    priced = [d for c in artifact.cards for d in c.dispatches]
    assert priced, "the stub drives at least one dispatch"
    for dispatch in priced:
        assert dispatch.tokens_processed == 0
        assert dispatch.cost_usd == 0.0
        assert dispatch.cost_basis == BASIS_NO_MODEL_CALLS
    assert artifact.totals.cost_usd == 0.0
    assert artifact.totals.cost_priced is True
    assert artifact.totals.cost_partial is False


def test_a_real_run_keeps_its_transcripts_under_its_run_directory(tmp_path: Path) -> None:
    collector = _run_transcripts(tmp_path / "run", None).collector

    assert collector.enabled is True
    assert collector.root == tmp_path / "run" / "transcripts"
