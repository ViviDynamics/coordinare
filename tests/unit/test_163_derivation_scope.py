"""163: every role reports exhaustion without widening retry eligibility."""
from __future__ import annotations

import pytest

from coordinare.graph.nodes.monitor_performer import monitor_performer
from tests.unit.graph.nodes.test_monitor_performer import _make_state, _Performer

STAGES = ['assessing', 'architecting', 'implementing', 'reviewing', 'security', 'qa', 'documenting', 'closing', 'env_bootstrap']


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', STAGES)
@pytest.mark.parametrize('metadata', [{'finish_reason': 'length'}, {'stop_reason': 'max_tokens'}, {}])
async def test_exhaustion_blocks_without_unchanged_retry(stage, metadata):
    reason = 'malformed JSON' if metadata else 'malformed JSON: finish_reason=length'
    state = _make_state(service=_Performer({'status': 'error', 'reason': reason, **metadata}), stage=stage)
    result = await monitor_performer(state)
    assert result['phase'] == 'blocked'
    assert 'output token cap' in result['open_questions'][0]
    assert result.get('system_error_count', 0) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', [s for s in STAGES if s != 'assessing'])
@pytest.mark.parametrize('reason', ['empty response body', 'malformed body'])
async def test_non_assessor_parse_shapes_do_not_gain_retries(stage, reason):
    state = _make_state(service=_Performer({'status': 'error', 'reason': reason}), stage=stage)
    result = await monitor_performer(state)
    assert result['phase'] == 'blocked'
    assert result.get('system_error_count', 0) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 3])
async def test_persisted_truncation_never_retries_unchanged(count):
    from coordinare.graph.nodes.handle_system_error import handle_system_error

    state = {"phase": "system_error", "performer_stage": "reviewing",
             "system_error_reason": "BACKEND_FORMAT_ERROR: finish_reason=length",
             "system_error_count": count}
    result = await handle_system_error(state)
    assert result["phase"] == "blocked"
    assert "output budget" in result["open_questions"][0]
    assert result["system_error_count"] == count


@pytest.mark.asyncio
async def test_null_reason_retains_empty_body_classification():
    import structlog.testing

    state = _make_state(service=_Performer({'status': 'error', 'reason': None}), stage='reviewing')
    with structlog.testing.capture_logs() as events:
        await monitor_performer(state)
    assert any(event['event'] == 'performer.parse_failure' and event['shape'] == 'empty_body' for event in events)
    assert not any(event['event'] == 'assessor.parse_failure' for event in events)
