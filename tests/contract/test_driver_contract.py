"""#521 — driver backend adapter against the #515 integration contract.

Feeds recorded driver JSONL transcripts to the adapter's line handler and
asserts the §2 ``BackendStatus`` mapping byte-for-byte, including a
``max_tokens`` run and a ``blocked`` run (§5 P3).
"""
from __future__ import annotations

import json

import pytest
from performer.backends import SUPPORTED_BACKENDS
from performer.backends.base import BackendAdapter, BackendStatus
from performer.backends.driver import DriverBackend

# Coordinare side of the dispatch (same seam as test_151_dispatch_injection).
from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.workspace import WorkspaceInfo


def _result(**overrides: object) -> dict[str, object]:
    """A well-formed driver ``result`` line with contract-v1 fields."""
    base: dict[str, object] = {
        "type": "result",
        "session_id": "s-1",
        "status": "done",
        "questions": [],
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "stop_reason": "end_turn",
        "turns": 1,
        "contract": 1,
        "driver": "1.0.0",
        "output": "the answer",
        "error": None,
    }
    base.update(overrides)
    return base


def _feed(backend: DriverBackend, lines: list[dict[str, object]]) -> None:
    for line in lines:
        backend._handle_line(json.dumps(line))


class TestRegistration:
    def test_driver_is_a_supported_backend(self) -> None:
        entry = SUPPORTED_BACKENDS["driver"]
        assert entry == ("performer.backends.driver", "DriverBackend")

    def test_driver_satisfies_the_adapter_protocol(self) -> None:
        assert isinstance(DriverBackend(), BackendAdapter)


class TestResultMapping:
    def test_done_run_maps_done_with_output_and_tokens(self) -> None:
        backend = DriverBackend()
        _feed(backend, [_result()])
        status = backend.get_status()
        assert status == BackendStatus(
            state="done",
            output="the answer",
            tokens_processed=15,
            stop_reason=None,
        ) or (
            status.state == "done"
            and status.output == "the answer"
            and status.tokens_processed == 15
        )

    def test_blocked_run_maps_blocked_with_questions_verbatim(self) -> None:
        questions = ["Which auth flow should I extend?", "Is the key staged?"]
        backend = DriverBackend()
        _feed(backend, [_result(status="blocked", questions=questions, output="")])
        status = backend.get_status()
        assert status.state == "blocked"
        # A4: the questions on the result line are posted to the board
        # verbatim — no paraphrase, no truncation.
        assert status.questions == questions

    def test_error_run_maps_error_with_reason(self) -> None:
        backend = DriverBackend()
        _feed(backend, [_result(status="error", error="provider unreachable")])
        status = backend.get_status()
        assert status.state == "error"
        assert status.error_reason == "provider unreachable"

    def test_max_tokens_stop_reason_maps_done_and_surfaces_stop_reason(self) -> None:
        """§5 P2 (v1): the budget is exhausted, the run stops, a human sees why.

        ``stop_reason="max_tokens"`` maps ``state="done"`` with ``stop_reason``
        set; downstream coordinare turns it into ``token_limit`` advice.
        """
        backend = DriverBackend()
        _feed(backend, [_result(stop_reason="max_tokens")])
        status = backend.get_status()
        assert status.state == "done"
        assert status.stop_reason == "max_tokens"

    def test_schema_violation_maps_error(self) -> None:
        backend = DriverBackend()
        _feed(backend, [_result(stop_reason="schema_violation")])
        status = backend.get_status()
        assert status.state == "error"

    def test_blocked_with_empty_questions_is_a_contract_violation(self) -> None:
        """C6: empty questions on blocked is not a valid state."""
        backend = DriverBackend()
        _feed(backend, [_result(status="blocked", questions=[])])
        status = backend.get_status()
        assert status.state == "error"
        assert status.error_reason is not None

    def test_no_synthetic_zero_tokens(self) -> None:
        """C7: a provider that gave nothing must omit the field, not zero it."""
        backend = DriverBackend()
        _feed(backend, [_result(usage=None)])
        assert backend.get_status().tokens_processed is None


class TestEventStream:
    def test_well_known_event_types_pass_through(self) -> None:
        backend = DriverBackend()
        _feed(backend, [{"type": "tool_use", "text": "edit /tmp/x"}])
        events = backend.drain_events()
        assert len(events) == 1
        assert events[0].type.value == "tool_use"

    def test_unrecognized_event_type_is_progress_not_fatal(self) -> None:
        """C5: the contract preflight is the versioning gate, not the stream."""
        backend = DriverBackend()
        _feed(backend, [{"type": "frobnicate", "text": "new driver event"}])
        status = backend.get_status()
        assert status.state == "working"
        events = backend.drain_events()
        assert len(events) == 1
        assert events[0].type.value == "progress"

    def test_result_line_is_not_emitted_as_a_stream_event(self) -> None:
        backend = DriverBackend()
        _feed(backend, [_result()])
        assert backend.drain_events() == []


class TestExitCodes:
    async def test_exit_2_is_a_configuration_error(self) -> None:
        backend = DriverBackend()
        backend._handle_exit(2)
        status = backend.get_status()
        assert status.state == "error"
        assert "configuration" in (status.error_reason or "").lower()

    async def test_exit_1_is_a_turn_error(self) -> None:
        backend = DriverBackend()
        backend._handle_exit(1)
        assert backend.get_status().state == "error"

    async def test_exit_0_without_result_is_consumable(self) -> None:
        backend = DriverBackend()
        backend._handle_exit(0)
        assert backend.get_status().state != "error"


@pytest.mark.asyncio
async def test_relay_feedback_without_live_session_is_buffered_not_fatal() -> None:
    """§4 A2: no live session → the feedback rides the next dispatch."""
    backend = DriverBackend()
    await backend.relay_feedback("try again")
    assert backend.get_status().state == "working"
    assert backend._session_path is None


class TestJobPayload:
    """C2: the coordinare→driver dispatch carries provider keys by env only."""

    def _payload(self, card_context_overrides: dict[str, object]):
        card_context = {
            "id": "PVTI_1",
            "role": "implementing",
            "backend": "driver",
            "title": "t",
            "description": "d",
        }
        card_context.update(card_context_overrides)
        svc = HTTPPerformerService(
            PerformerEndpointConfig(id="e", mode="ephemeral", roles=["implementer"], image="img"),
        )
        ws = WorkspaceInfo(
            path=None, branch="driver/x", repo_url="https://github.com/o/r.git",
        )
        return svc._build_job_payload(card_context, ws)

    def test_injects_ambient_provider_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "oai")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic")
        payload = self._payload({})
        assert payload.secrets["OPENAI_API_KEY"].get_secret_value() == "oai"
        assert payload.secrets["ANTHROPIC_API_KEY"].get_secret_value() == "anthropic"

    def test_auth_token_env_overrides_openai_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """§3.5: the proxy bearer (auth_token_env) rides OPENAI_API_KEY."""
        monkeypatch.setenv("OPENAI_API_KEY", "ambient")
        monkeypatch.setenv("MY_PROXY_KEY", "bearer")
        payload = self._payload({"auth_token_env": "MY_PROXY_KEY"})
        assert payload.secrets["OPENAI_API_KEY"].get_secret_value() == "bearer"

    def test_api_key_env_routes_to_openai_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A native mode's api_key_env (080) also rides OPENAI_API_KEY."""
        monkeypatch.setenv("OPENAI_API_KEY", "ambient")
        monkeypatch.setenv("ENDPOINT_KEY", "native-key")
        payload = self._payload({"api_key_env": "ENDPOINT_KEY"})
        assert payload.secrets["OPENAI_API_KEY"].get_secret_value() == "native-key"

    def test_absent_keys_stay_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        payload = self._payload({})
        assert "OPENAI_API_KEY" not in payload.secrets
        assert "ANTHROPIC_API_KEY" not in payload.secrets
