"""Spec 242 review fixes: the bench's transcript guard, retention and redaction; a cut-off
run's tail is drained at close or marked not fully drained; the message and run usage
cross-check; the conductor-scope label; and the injected-services gap.

Written test-first: each test names the review finding it closes.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from coordinare.bench.artifact import RunArtifact
from coordinare.bench.pricing import bench_price_table, price_dispatch
from coordinare.bench.runner import _attach_transcript_usage, _run_transcripts, _RunTranscripts
from coordinare.config import CostTrackingConfig, ModelPriceConfig, ProjectConfiguration
from coordinare.models.performer_endpoint import JobStatus, PerformerEndpointConfig
from coordinare.services import run_transcript
from coordinare.services.http_performer_service import (
    UNDRAINED_REASON,
    HTTPPerformerService,
    _EphemeralJob,
)
from coordinare.services.run_transcript import TranscriptCollector, make_collector
from coordinare.services.token_usage import (
    ModelPrice,
    PriceTable,
    TokenLedger,
    UsageCounts,
    price_totals,
)
from tests.unit.test_242_bench_exact_pricing import _build, _dispatch_log, _usage_record

OPUS = "claude-opus-4-5"
_MILLION = 1_000_000

_MINIMAL: dict[str, Any] = {
    "project_name": "acme",
    "github_org": "acme",
    "github_project_number": 1,
    "github_token": "ghp_test_token_value",
    "human_reviewers": ["reviewer1"],
}

TABLE = bench_price_table(CostTrackingConfig(
    pricing_as_of=datetime(2026, 10, 1, tzinfo=UTC).date(),
    prices=[ModelPriceConfig(
        models=["claude-opus-4-5"], input_per_million=15.0, output_per_million=75.0,
        cache_read_per_million=1.5, cache_write_per_million=18.75,
    )],
))


def _config(**extra: Any) -> ProjectConfiguration:
    return ProjectConfiguration(**_MINIMAL, **extra)


# --- BLOCK 1: the bench collector is guarded like the daemon's ---------------------------


def test_performer_mount_over_the_run_directory_disables_the_bench_collector(tmp_path: Path) -> None:
    """A performer volume covering the run's parent would let a container see the transcripts."""
    run_dir = tmp_path / "run"
    config = _config(performer_log_dir=str(tmp_path))

    transcripts = _run_transcripts(run_dir, config, None)

    assert transcripts.collector.enabled is False
    assert transcripts.disabled_reason is not None
    assert "performer mount" in transcripts.disabled_reason


def test_a_disabled_bench_collector_makes_the_run_cost_unknown_with_that_reason(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    transcripts = _run_transcripts(run_dir, _config(performer_log_dir=str(tmp_path)), None)
    log = [_dispatch_log("sess-blocked")]

    _attach_transcript_usage(log, transcripts)
    artifact = _build(log)

    dispatch = artifact.cards[0].dispatches[0]
    assert dispatch.cost_usd is None
    assert dispatch.cost_unknown_reason is not None
    assert "performer mount" in dispatch.cost_unknown_reason
    assert artifact.totals.cost_usd is None


def test_the_bench_guard_protects_the_run_directory_itself(tmp_path: Path) -> None:
    """The root may not contain the run directory: a symlink or a stray folder must not swallow it."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    transcripts = _run_transcripts(run_dir, _config(transcripts={"dir": str(run_dir.parent)}), None)

    assert transcripts.collector.enabled is False
    assert transcripts.disabled_reason is not None


def test_transcripts_disabled_in_config_makes_cost_unknown_with_that_reason(tmp_path: Path) -> None:
    transcripts = _run_transcripts(tmp_path / "run", _config(transcripts={"enabled": False}), None)
    log = [_dispatch_log("sess-off")]

    _attach_transcript_usage(log, transcripts)
    artifact = _build(log)

    assert transcripts.collector.enabled is False
    assert artifact.cards[0].dispatches[0].cost_unknown_reason == transcripts.disabled_reason
    assert transcripts.disabled_reason is not None
    assert transcripts.disabled_reason.startswith("transcripts disabled")


def test_transcripts_dir_from_config_is_the_bench_root(tmp_path: Path) -> None:
    custom = tmp_path / "elsewhere"

    transcripts = _run_transcripts(tmp_path / "run", _config(transcripts={"dir": str(custom)}), None)

    assert transcripts.collector.root == custom
    assert transcripts.collector.enabled is True


def test_retention_prunes_expired_bench_transcripts_at_the_start_of_a_run(tmp_path: Path) -> None:
    """Bench transcripts follow the daemon's retention, so an old run file does not live forever."""
    root = tmp_path / "run" / "transcripts"
    old = root / "alpha" / "PVTI_1" / "old-run.jsonl"
    old.parent.mkdir(parents=True)
    old.write_text("{}\n", encoding="utf-8")
    long_ago = (datetime.now(UTC) - timedelta(days=400)).timestamp()
    os.utime(old, (long_ago, long_ago))

    _run_transcripts(tmp_path / "run", _config(), None)

    assert not old.exists()


def test_bench_collector_gets_the_ambient_secrets_as_the_daemon_does(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    seen: list[Any] = []
    real = TranscriptCollector.add_ambient_secrets

    def spy(self: TranscriptCollector, environ: Any) -> None:
        seen.append(environ)
        real(self, environ)

    monkeypatch.setattr(TranscriptCollector, "add_ambient_secrets", spy)

    _run_transcripts(tmp_path / "run", _config(), None)

    assert len(seen) == 1
    assert seen[0] is os.environ


# --- injected performer services without a collector: unknown, not silently unpriced -----


def test_injected_services_without_a_bench_collector_make_cost_unknown_with_a_reason() -> None:
    log = [_dispatch_log("sess-injected")]

    _attach_transcript_usage(log, None)
    artifact = _build(log)

    reason = artifact.cards[0].dispatches[0].cost_unknown_reason
    assert reason is not None
    assert "performer services were injected" in reason
    assert artifact.totals.cost_usd is None


# --- BLOCK 2: a cut-off run's tail is drained at close, or marked not fully drained ------


class _PollingClient:
    """A performer that holds records the daemon never polled. ``fail`` makes every drain fail."""

    def __init__(self, records: list[dict[str, Any]], *, fail: bool = False) -> None:
        self._records = records
        self._fail = fail

    async def get_job(
        self, job_id: str, *, transcript_after: int | None = None, transcript_limit: int = 200,
    ) -> JobStatus:
        if self._fail:
            raise ValueError("performer unreachable during close")
        after = int(transcript_after or 0)
        batch = [r for r in self._records if r["seq"] > after]
        return JobStatus(
            job_id=job_id, state="running", started_at=datetime.now(UTC),
            transcript=batch, transcript_last_seq=len(self._records),
        )


class _Runtime:
    async def stop(self, handle: str, **_kw: Any) -> None:
        return None


def _ephemeral_config() -> PerformerEndpointConfig:
    return PerformerEndpointConfig.model_validate({
        "id": "perf-242", "mode": "ephemeral", "roles": ["implementing"],
        "image": "performer:base", "endpoint": "http://127.0.0.1:8080",
    })


def _cut_off_service(
    tmp_path: Path, client: _PollingClient,
) -> tuple[HTTPPerformerService, TranscriptCollector, str]:
    collector = make_collector(
        enabled=True, root=tmp_path / "t", max_bytes_per_run=10**6, retention_days=30,
        guard=run_transcript.RootGuard(),
    )
    svc = HTTPPerformerService(_ephemeral_config(), runtime=cast("Any", _Runtime()), transcripts=collector)
    session = "sess-cut-off"
    svc._transcript_targets[session] = ("alpha", "PVTI_c", "implementing")
    svc._active_jobs[session] = _EphemeralJob(
        container_id="cid-1", endpoint="http://127.0.0.1:8080", client=cast("Any", client),
        job_id="job-1", run_id=session,
    )
    return svc, collector, session


async def test_aclose_drains_the_unpolled_tail_before_stopping_and_the_ledger_has_it(tmp_path: Path) -> None:
    records = [
        _usage_record(1, OPUS, input_tokens=_MILLION, output_tokens=0),
        _usage_record(2, OPUS, input_tokens=0, output_tokens=100_000),
    ]
    svc, collector, session = _cut_off_service(tmp_path, _PollingClient(records))

    await svc.aclose()

    totals = collector.usage.totals(run_id=session)
    assert totals[OPUS] == UsageCounts(input_tokens=_MILLION, output_tokens=100_000)
    assert svc.transcript_gap(session) is None


async def test_a_tail_that_cannot_be_drained_at_close_is_marked_not_fully_drained(tmp_path: Path) -> None:
    records = [_usage_record(1, OPUS, input_tokens=_MILLION)]
    svc, collector, session = _cut_off_service(tmp_path, _PollingClient(records, fail=True))

    await svc.aclose()

    assert svc.transcript_gap(session) == UNDRAINED_REASON
    assert collector.usage.totals(run_id=session) == {}


def test_a_dispatch_with_an_undrained_tail_is_never_priced_exact() -> None:
    usage = {OPUS: UsageCounts(input_tokens=_MILLION)}

    priced = price_dispatch(usage, None, None, TABLE, gap=UNDRAINED_REASON)

    assert priced.cost_usd is None
    assert priced.unknown_reason == UNDRAINED_REASON
    assert priced.basis is None


async def test_an_undrained_run_in_a_bench_artifact_is_unknown_with_the_reason(tmp_path: Path) -> None:
    records = [_usage_record(1, OPUS, input_tokens=_MILLION)]
    svc, _collector, session = _cut_off_service(tmp_path, _PollingClient(records, fail=True))
    await svc.aclose()
    log = [_dispatch_log(session)]

    _attach_transcript_usage(log, _as_transcripts(_collector_of(svc)), undrained={session: UNDRAINED_REASON})
    artifact = _build(log)

    assert artifact.cards[0].dispatches[0].cost_unknown_reason == UNDRAINED_REASON
    assert artifact.cards[0].dispatches[0].cost_usd is None


def _collector_of(svc: HTTPPerformerService) -> TranscriptCollector:
    collector = svc._transcripts
    assert collector is not None
    return collector


def _as_transcripts(collector: TranscriptCollector) -> _RunTranscripts:
    return _RunTranscripts(collector=collector, disabled_reason=None)


# --- message versus run usage: the larger total wins, and the disagreement is named -------


def _ledger_with(records: list[dict[str, Any]]) -> TokenLedger:
    ledger = TokenLedger(strict_totals=True)
    ledger.observe("run-x", symphony="alpha", card="PVTI_x", stage="implementing", records=records)
    return ledger


def _run_record(seq: int, model: str, total: int) -> dict[str, Any]:
    return {
        "type": "usage", "seq": seq, "ts": "2026-10-14T10:00:00+00:00", "scope": "run",
        "usage": {"model": model, "input_tokens": total},
    }


def test_when_the_run_total_is_larger_than_the_message_sum_the_larger_is_used_and_flagged() -> None:
    ledger = _ledger_with([
        _usage_record(1, OPUS, input_tokens=100),  # message scope, a lost record makes it low
        _run_record(2, OPUS, 150),
    ])

    totals = ledger.totals(run_id="run-x")

    assert totals[OPUS] == UsageCounts(input_tokens=150)
    reason = ledger.run_disagreement("run-x")
    assert reason is not None
    assert "100" in reason and "150" in reason


def test_when_the_message_sum_is_larger_it_is_used_and_still_flagged() -> None:
    ledger = _ledger_with([
        _usage_record(1, OPUS, input_tokens=200),
        _run_record(2, OPUS, 150),
    ])

    assert ledger.totals(run_id="run-x")[OPUS] == UsageCounts(input_tokens=200)
    assert ledger.run_disagreement("run-x") is not None


def test_agreeing_message_and_run_totals_are_not_flagged() -> None:
    ledger = _ledger_with([
        _usage_record(1, OPUS, input_tokens=150),
        _run_record(2, OPUS, 150),
    ])

    assert ledger.run_disagreement("run-x") is None


def test_a_disagreement_makes_the_dispatch_unknown_with_its_reason(tmp_path: Path) -> None:
    collector = make_collector(
        enabled=True, root=tmp_path / "t", max_bytes_per_run=10**6, retention_days=30,
        guard=run_transcript.RootGuard(),
    )
    collector.use_strict_totals()
    collector.usage.observe("sess-d", symphony="alpha", card="PVTI_d", stage="implementing", records=[
        _usage_record(1, OPUS, input_tokens=100), _run_record(2, OPUS, 150),
    ])
    log = [_dispatch_log("sess-d")]

    _attach_transcript_usage(log, _as_transcripts(collector))
    artifact = _build(log)

    reason = artifact.cards[0].dispatches[0].cost_unknown_reason
    assert reason is not None
    assert "message usage sums to" in reason


# --- conductor spend is labelled as outside the bench cost ------------------------------


def test_the_artifact_states_that_its_cost_covers_performers_only() -> None:
    artifact = _build([_dispatch_log(None, tokens=None)])

    assert isinstance(artifact, RunArtifact)
    assert artifact.cost_scope == "performers only"


# --- the daemon keeps main's precedence; only the bench's strict mode takes the larger total ---

_S = "claude-sonnet-4-5"
_H = "claude-haiku-4-5"
_PRICES = PriceTable(
    rules={
        _S: ModelPrice(3.0, 15.0, 0.3, 3.75),
        _H: ModelPrice(1.0, 5.0, 0.1, 1.25),
    },
    source="table",
)


def _multi_model_message_records() -> list[dict[str, Any]]:
    """A claude_code multi-model run: one message record per model turn, each with its model."""
    return [
        _usage_record(1, _S, input_tokens=10, output_tokens=60, cache_read=1000, cache_write=50),
        _usage_record(2, _H, input_tokens=900, output_tokens=40, cache_read=0, cache_write=900),
        _usage_record(3, _S, input_tokens=4, output_tokens=300, cache_read=9000, cache_write=0),
    ]


def _model_less_run_record(seq: int, *, scale_input: int) -> dict[str, Any]:
    """The run-scope record of a multi-model run: no model, so its total is not priced per model.

    ``scale_input`` raises the input total, as a subagent call outside the message stream would.
    """
    return {
        "type": "usage", "seq": seq, "ts": "2026-10-14T10:00:00+00:00", "scope": "run",
        "usage": {"input_tokens": 914 + scale_input, "output_tokens": 400,
                  "cache_read": 10000, "cache_write": 950},
    }


def test_the_daemon_ledger_keeps_message_precedence_when_the_run_total_is_two_percent_larger() -> None:
    """Regression (review of 242): a model-less run total 2% above the message sum must not
    replace the message figures on the daemon's ledger. The cost is the one main prices."""
    messages = _multi_model_message_records()
    reference = TokenLedger()
    reference.observe("run-m", symphony="alpha", card="PVTI_m", stage="implementing", records=messages)
    daemon = TokenLedger()
    daemon.observe(
        "run-m", symphony="alpha", card="PVTI_m", stage="implementing",
        records=[*messages, _model_less_run_record(4, scale_input=218)],
    )

    expected = price_totals(reference.totals(run_id="run-m"), _PRICES)
    actual = price_totals(daemon.totals(run_id="run-m"), _PRICES)

    assert actual.tokens == expected.tokens
    assert actual.cost_usd is not None
    assert actual.cost_usd == expected.cost_usd
    assert actual.unknown_reason is None
    assert daemon.run_disagreement("run-m") is None


def test_the_strict_bench_ledger_takes_the_larger_total_and_names_the_gap() -> None:
    messages = _multi_model_message_records()
    strict = TokenLedger(strict_totals=True)
    strict.observe(
        "run-s", symphony="alpha", card="PVTI_s", stage="implementing",
        records=[*messages, _model_less_run_record(4, scale_input=218)],
    )

    assert strict.totals(run_id="run-s")[None].input_tokens == 1132
    reason = strict.run_disagreement("run-s")
    assert reason is not None
    assert "12264" in reason and "12482" in reason


def test_the_multi_model_run_total_is_unknown_in_the_bench_and_never_priced_from_the_model_less_total(
    tmp_path: Path,
) -> None:
    collector = make_collector(
        enabled=True, root=tmp_path / "t", max_bytes_per_run=10**6, retention_days=30,
        guard=run_transcript.RootGuard(),
    )
    collector.use_strict_totals()
    collector.usage.observe(
        "sess-multi", symphony="alpha", card="PVTI_x", stage="implementing",
        records=[*_multi_model_message_records(), _model_less_run_record(4, scale_input=218)],
    )
    log = [_dispatch_log("sess-multi")]

    _attach_transcript_usage(log, _as_transcripts(collector))
    artifact = _build(log)

    dispatch = artifact.cards[0].dispatches[0]
    assert dispatch.cost_usd is None
    assert dispatch.cost_unknown_reason is not None
    assert "message usage sums to" in dispatch.cost_unknown_reason

