"""Unknown performer usage stays unknown all the way into the benchmark artifact.

#305: completed Claude Code dispatches reported ``metrics.tokens_processed: 0``
despite successful model calls, so the artifact summed a measured zero, priced it
at $0.00 and left ``cost_component_missing`` false. Zero appeared measured when it
was only unmeasured.

These tests walk the whole chain a live dispatch takes, from a realistic Claude
Code CLI ``result`` event through the performer's terminal ``PerformerResponse``,
the bench recorder, the dispatch-log join and the artifact rollup, and pin both
ends of the contract: real usage arrives nonzero, and absent usage arrives as an
explicit missing value rather than a zero.
"""
from __future__ import annotations

from typing import Any

import pytest

from coordinare.bench.artifact import estimate_cost_usd
from coordinare.bench.harness_rank import score_row
from coordinare.bench.harness_rollup import RoleHarnessRow
from coordinare.bench.recording_performer import RecordingPerformer
from coordinare.bench.runner import _records_to_dispatch_log

# A cache-heavy turn, the normal shape for Claude Code: almost all of the input is
# cache reads, so input_tokens alone is a rounding error against the real total.
CACHE_HEAVY_USAGE = {
    "input_tokens": 4,
    "cache_creation_input_tokens": 12_000,
    "cache_read_input_tokens": 88_000,
    "output_tokens": 900,
}
CACHE_HEAVY_TOTAL = 100_904


def _backend_tokens(event: dict[str, Any]) -> int | None:
    """Drive the real claude_code result parser, not a hand-built number."""
    from performer.backends.claude_code import ClaudeCodeBackend

    backend = ClaudeCodeBackend()
    backend._handle_event(event)
    return backend.get_status().tokens_processed


class _TerminalPerformer:
    """A performer whose terminal poll returns the PerformerResponse coordinare's
    ``HTTPPerformerService.check_status`` reconstructs from ``JobResult.summary``."""

    def __init__(self, metrics: dict[str, Any] | None) -> None:
        self._metrics = metrics
        self.polls = 0

    async def dispatch_card(self, _ctx: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        return {"status": "ok", "session_id": "s1", "job_id": "j1"}

    async def check_status(self, _sid: str, **_kw: Any) -> dict[str, Any]:
        self.polls += 1
        if self.polls == 1:
            return {"status": "working", "job_id": "j1"}
        response: dict[str, Any] = {"status": "pr_opened"}
        if self._metrics is not None:
            response["metrics"] = self._metrics
        return response


async def _dispatch_row(metrics: dict[str, Any] | None) -> dict[str, Any]:
    svc = RecordingPerformer(_TerminalPerformer(metrics))
    await svc.dispatch_card({"stage": "implementing", "role": "implementer", "item_id": "PVTI_1"})
    await svc.check_status("s1")
    await svc.check_status("s1")
    return _records_to_dispatch_log({"implementing": svc})[0]


@pytest.mark.asyncio
async def test_real_cli_usage_reaches_the_artifact_as_measured_nonzero() -> None:
    tokens = _backend_tokens({
        "type": "result", "subtype": "success", "session_id": "sess-1",
        "total_cost_usd": 0.42, "usage": CACHE_HEAVY_USAGE,
    })
    assert tokens == CACHE_HEAVY_TOTAL  # cache tokens counted, not dropped

    row = await _dispatch_row({"tokens_processed": tokens})
    assert row["terminal_marker"] == "pr_opened"
    assert row["tokens_processed"] == CACHE_HEAVY_TOTAL
    assert estimate_cost_usd(row["tokens_processed"], 3.0) > 0


@pytest.mark.asyncio
async def test_absent_cli_usage_reaches_the_artifact_as_explicitly_missing() -> None:
    """The live failure: a terminal result with no usage (a proxy stripped it).

    The dispatch must still be recorded, with tokens as None rather than 0, so the
    artifact reports unknown cost instead of a free run.
    """
    tokens = _backend_tokens({"type": "result", "subtype": "success", "session_id": "sess-2"})
    assert tokens is None

    row = await _dispatch_row({"tokens_processed": tokens})
    assert row["terminal_marker"] == "pr_opened"   # the dispatch is still observed
    assert row["tokens_processed"] is None         # but its usage is unknown
    assert estimate_cost_usd(row["tokens_processed"], 3.0) is None


@pytest.mark.asyncio
async def test_terminal_response_without_metrics_is_unknown_not_zero() -> None:
    row = await _dispatch_row(None)
    assert row["terminal_marker"] == "pr_opened"
    assert row["tokens_processed"] is None


def test_unknown_tokens_flag_the_cost_component_missing() -> None:
    """The scorer must be able to tell unmeasured from free. A zero here is a
    priced $0.00 that silently flatters an unmeasured harness."""
    unknown = score_row(
        RoleHarnessRow(role="implementer", backend="claude_code",
                       credit_rate=1.0, seconds_total=100.0, tokens_total=None),
        cost_per_million_tokens=3.0,
    )
    assert unknown.cost_component_missing is True

    measured = score_row(
        RoleHarnessRow(role="implementer", backend="claude_code", credit_rate=1.0,
                       seconds_total=100.0, tokens_total=CACHE_HEAVY_TOTAL),
        cost_per_million_tokens=3.0,
    )
    assert measured.cost_component_missing is False
