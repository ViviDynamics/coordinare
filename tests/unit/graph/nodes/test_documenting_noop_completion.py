"""Spec 126 US3 — documenting no-op completions (contract U1/U2).

A ``docs_committed`` that changed nothing advances the stage but never mints
a documentation pass (keeps 125's last-documented SHA truthful). A real pass
records as today.

Contract: specs/126-terminal-success-floors/contracts/success-floors.md
"""
from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.state import initial_state


class _Performer:
    def __init__(self, response: dict) -> None:
        self._response = response

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        _ = session_id
        return self._response


def _doc_state(response: dict) -> dict:
    state = initial_state()
    svc = _Performer(response=response)
    state["performer_services"] = {"documenting": svc, "closing_review": _Performer({})}
    state["performer_stage"] = "documenting"
    state["lifecycle_sequence"] = ["documenting", "closing_review"]
    state["current_card"] = {"id": "ITEM_DOC", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["head_at_dispatch"] = "before111"
    return state


@pytest.mark.asyncio
async def test_real_doc_pass_records_verdict_as_today() -> None:
    # U1: modified files + head delta -> 125 records the documentation pass.
    state = _doc_state({
        "status": "docs_committed",
        "files_modified": ["docs/wiki/setup.md"],
        "head_after": "after222",
    })

    result = await monitor_performer(state)

    assert result["performer_stage"] == "closing_review"
    assert result["stage_verdicts"]["documenting"]["head_sha"] == "after222"


@pytest.mark.asyncio
async def test_noop_doc_completion_advances_without_recording() -> None:
    # U2: zero files, no head delta -> advance + event + NO verdict record.
    state = _doc_state({
        "status": "docs_committed",
        "files_modified": [],
        "head_after": "before111",
    })

    with capture_logs() as logs:
        result = await monitor_performer(state)

    assert result["performer_stage"] == "closing_review"
    assert "documenting" not in (result.get("stage_verdicts") or {})
    assert [
        e for e in logs
        if e.get("event") == "monitor_performer.documenting_noop_completion"
    ]


@pytest.mark.asyncio
async def test_noop_completion_with_head_delta_still_records() -> None:
    # A committed head delta means real work even if files_modified is empty
    # (defensive: some backends under-report files) — record as today.
    state = _doc_state({
        "status": "docs_committed",
        "files_modified": [],
        "head_after": "after333",
    })

    result = await monitor_performer(state)

    assert result["stage_verdicts"]["documenting"]["head_sha"] == "after333"


@pytest.mark.asyncio
async def test_later_real_pass_records_after_noop() -> None:
    # U2 then U1: the no-op never poisons later recording.
    state = _doc_state({
        "status": "docs_committed",
        "files_modified": [],
        "head_after": "before111",
    })
    result = await monitor_performer(state)
    assert "documenting" not in (result.get("stage_verdicts") or {})

    result["performer_stage"] = "documenting"
    result["agent_dispatch"] = {"session_id": "s2"}
    result["performer_services"]["documenting"] = _Performer({
        "status": "docs_committed",
        "files_modified": ["docs/wiki/setup.md"],
        "head_after": "after444",
    })
    result2 = await monitor_performer(result)

    assert result2["stage_verdicts"]["documenting"]["head_sha"] == "after444"
