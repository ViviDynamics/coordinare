"""Unit tests for performer.protocol — constitution Principle II."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from performer.protocol import (
    PerformerMessage,
    PerformerMetrics,
    PerformerResponse,
)


class TestPerformerMessage:
    def test_accepts_dispatch(self) -> None:
        msg = PerformerMessage(action="dispatch")
        assert msg.action == "dispatch"

    def test_accepts_status(self) -> None:
        msg = PerformerMessage(action="status", session_id="abc")
        assert msg.action == "status"

    def test_accepts_relay_feedback(self) -> None:
        msg = PerformerMessage(action="relay_feedback", payload={"feedback": "go ahead"})
        assert msg.action == "relay_feedback"

    def test_accepts_health(self) -> None:
        msg = PerformerMessage(action="health")
        assert msg.action == "health"

    def test_rejects_unknown_action(self) -> None:
        with pytest.raises(ValidationError):
            PerformerMessage(action="unknown_action")  # type: ignore[arg-type]

    def test_defaults(self) -> None:
        msg = PerformerMessage(action="health")
        assert msg.session_id == ""
        assert msg.payload == {}


class TestPerformerResponse:
    @pytest.mark.parametrize(
        "status",
        [
            "accepted", "working", "pr_opened", "blocked", "error",
            "unknown", "busy", "acknowledged", "session_expired",
            "healthy", "unhealthy",
        ],
    )
    def test_serialises_all_status_literals(self, status: str) -> None:
        resp = PerformerResponse(status=status)  # type: ignore[arg-type]
        dumped = json.loads(resp.model_dump_json())
        assert dumped["status"] == status

    def test_metrics_none_omitted_from_json(self) -> None:
        resp = PerformerResponse(status="working", metrics=None)
        dumped = json.loads(resp.model_dump_json(exclude_none=True))
        assert "metrics" not in dumped

    def test_metrics_present_in_json_when_set(self) -> None:
        metrics = PerformerMetrics(pid=42, child_pids=[43], memory_bytes=1024)
        resp = PerformerResponse(status="working", metrics=metrics)
        dumped = json.loads(resp.model_dump_json())
        assert dumped["metrics"]["pid"] == 42

    def test_defaults(self) -> None:
        resp = PerformerResponse(status="healthy")
        assert resp.session_id == ""
        assert resp.reason is None
        assert resp.questions == []
        assert resp.pr_url is None
        assert resp.pr_node_id is None
        assert resp.metrics is None


class TestPerformerMetrics:
    def test_all_fields_nullable(self) -> None:
        m = PerformerMetrics()
        assert m.pid is None
        assert m.memory_bytes is None
        assert m.cpu_percent is None
        assert m.tokens_processed is None
        assert m.child_pids == []

    def test_partial_fields(self) -> None:
        m = PerformerMetrics(pid=1, memory_bytes=512)
        assert m.pid == 1
        assert m.memory_bytes == 512
        assert m.cpu_percent is None
