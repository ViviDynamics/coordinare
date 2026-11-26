"""#360: the nare backend adapter against nare's machine contract 3.

Feeds nare JSONL lines, in the shapes a real ``nare run --jsonl`` writes at the
pinned release, to the adapter's line handler and asserts the ``BackendStatus``
mapping, including a ``max_tokens`` run and a ``blocked`` run.
"""
from __future__ import annotations

import json

import pytest
from performer.backends import SUPPORTED_BACKENDS
from performer.backends.base import BackendAdapter, BackendStatus
from performer.backends.nare import NareBackend

# Coordinare side of the dispatch (same seam as test_151_dispatch_injection).
from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.workspace import WorkspaceInfo


def _usage(**overrides: object) -> dict[str, object]:
    usage: dict[str, object] = {
        "input": 10, "output": 5, "cache_read": 0, "cache_write": 0,
        "cost": None, "estimated": False, "total": 15,
    }
    usage.update(overrides)
    return usage


def _result(**overrides: object) -> dict[str, object]:
    """A well-formed nare ``result`` line, contract 3 fields. ``output`` is the
    validated ``--schema`` document (null without one); ``text`` is the final reply."""
    base: dict[str, object] = {
        "type": "result",
        "session_id": "s-1",
        "status": "done",
        "questions": [],
        "usage": _usage(),
        "budget": {"used_tokens": 15, "used_usd": None, "used_tool_calls": 0},
        "stop_reason": "end_turn",
        "turns": 1,
        "retries": 0,
        "contract": 3,
        "nare": "2026.11.109",
        "output": None,
        "text": "the answer",
        "error": None,
        "failure": None,
    }
    base.update(overrides)
    return base


def _feed(backend: NareBackend, lines: list[dict[str, object]]) -> None:
    for line in lines:
        backend._handle_line(json.dumps(line))


class TestRegistration:
    def test_nare_is_a_supported_backend(self) -> None:
        entry = SUPPORTED_BACKENDS["nare"]
        assert entry == ("performer.backends.nare", "NareBackend")

    def test_driver_is_gone(self) -> None:
        """It never ran against a released nare, so it is removed, not aliased."""
        assert "driver" not in SUPPORTED_BACKENDS

    def test_nare_satisfies_the_adapter_protocol(self) -> None:
        assert isinstance(NareBackend(), BackendAdapter)


class TestResultMapping:
    def test_done_run_maps_done_with_output_and_tokens(self) -> None:
        backend = NareBackend()
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
        backend = NareBackend()
        _feed(backend, [_result(status="blocked", questions=questions, text=None, stop_reason="tool_use")])
        status = backend.get_status()
        assert status.state == "blocked"
        # A4: the questions on the result line are posted to the board
        # verbatim — no paraphrase, no truncation.
        assert status.questions == questions

    def test_error_run_maps_error_with_reason(self) -> None:
        backend = NareBackend()
        _feed(backend, [_result(status="error", error="provider unreachable")])
        status = backend.get_status()
        assert status.state == "error"
        assert status.error_reason == "provider unreachable"

    def test_max_tokens_stop_reason_maps_done_and_surfaces_stop_reason(self) -> None:
        """§5 P2 (v1): the budget is exhausted, the run stops, a human sees why.

        ``stop_reason="max_tokens"`` maps ``state="done"`` with ``stop_reason``
        set; downstream coordinare turns it into ``token_limit`` advice.
        """
        backend = NareBackend()
        _feed(backend, [_result(stop_reason="max_tokens")])
        status = backend.get_status()
        assert status.state == "done"
        assert status.stop_reason == "max_tokens"

    def test_schema_answer_is_the_validated_output_document(self) -> None:
        """Under --schema, ``output`` is the parsed document; it is the report."""
        backend = NareBackend()
        _feed(backend, [_result(output={"verdict": "pass"}, text='{"verdict": "pass"}')])
        assert json.loads(backend.get_status().output or "") == {"verdict": "pass"}

    def test_text_without_schema_is_the_report(self) -> None:
        """Without --schema, ``output`` is null and the reply is ``text``. Reading
        only ``output`` would hand every verdict role an empty report."""
        backend = NareBackend()
        _feed(backend, [_result(output=None, text='{"approved": true}')])
        assert backend.get_status().output == '{"approved": true}'

    def test_usage_total_counts_the_cache(self) -> None:
        """``usage.total`` is the count to use; input plus output undercounts."""
        backend = NareBackend()
        _feed(backend, [_result(usage=_usage(input=10, output=5, cache_read=100, total=115))])
        assert backend.get_status().tokens_processed == 115

    def test_tool_call_cap_names_the_budget(self) -> None:
        backend = NareBackend()
        _feed(backend, [_result(status="error", stop_reason="tool_calls", text=None,
                                error="the tool call cap was reached")])
        status = backend.get_status()
        assert status.state == "error"
        assert (status.error_reason or "").startswith("TOOL_BUDGET_EXHAUSTED")

    def test_blocked_with_empty_questions_is_a_contract_violation(self) -> None:
        """C6: empty questions on blocked is not a valid state."""
        backend = NareBackend()
        _feed(backend, [_result(status="blocked", questions=[])])
        status = backend.get_status()
        assert status.state == "error"
        assert status.error_reason is not None

    def test_no_synthetic_zero_tokens(self) -> None:
        """C7: a provider that gave nothing must omit the field, not zero it."""
        backend = NareBackend()
        _feed(backend, [_result(usage=None)])
        assert backend.get_status().tokens_processed is None
        backend = NareBackend()
        _feed(backend, [_result(usage=_usage(input=0, output=0, total=0))])
        assert backend.get_status().tokens_processed is None


class TestEventStream:
    def test_a_tool_call_carries_its_name_and_primary_argument(self) -> None:
        backend = NareBackend()
        _feed(backend, [{"type": "tool_use", "text": "bash", "tool_use_id": "c1",
                         "detail": {"command": "uv run pytest -q"}}])
        [event] = backend.drain_events()
        assert event.type.value == "tool_use"
        assert event.tool == "bash"
        assert event.arg == "uv run pytest -q"
        assert event.call_id == "c1"

    def test_a_tool_result_event_reports_its_outcome(self) -> None:
        backend = NareBackend()
        _feed(backend, [{"type": "progress", "text": "edit result: error", "tool_use_id": "c2",
                         "detail": {"tool": "edit", "tool_result": {
                             "tool_use_id": "c2", "is_error": True, "chars": 41}}}])
        [event] = backend.drain_events()
        assert event.type.value == "tool_use"
        assert event.status == "error"
        assert event.call_id == "c2"

    def test_cost_events_accumulate_tokens_while_working(self) -> None:
        backend = NareBackend()
        cost = {"type": "cost", "text": "100 in / 20 out", "detail": _usage(input=100, output=20, total=120)}
        _feed(backend, [cost, cost])
        status = backend.get_status()
        assert status.state == "working"
        assert status.tokens_processed == 240

    def test_unrecognized_event_type_is_output_not_fatal(self) -> None:
        """C5: the contract preflight is the versioning gate, not the stream.

        178: an unrecognized event is a raw line from nare, so it is ``output``, not narration.
        """
        backend = NareBackend()
        _feed(backend, [{"type": "frobnicate", "text": "new nare event"}])
        status = backend.get_status()
        assert status.state == "working"
        events = backend.drain_events()
        assert len(events) == 1
        assert events[0].type.value == "output"

    def test_result_line_is_not_emitted_as_a_stream_event(self) -> None:
        backend = NareBackend()
        _feed(backend, [_result()])
        assert backend.drain_events() == []


class TestExitCodes:
    async def test_exit_2_is_a_configuration_error(self) -> None:
        backend = NareBackend()
        backend._handle_exit(2)
        status = backend.get_status()
        assert status.state == "error"
        assert "configuration" in (status.error_reason or "").lower()

    async def test_exit_1_is_a_turn_error(self) -> None:
        backend = NareBackend()
        backend._handle_exit(1)
        assert backend.get_status().state == "error"

    async def test_exit_0_without_result_is_an_error(self) -> None:
        """nare writes a result line for every run that started; a run that
        ends without one crashed, and waiting on it would hang the card."""
        backend = NareBackend()
        backend._handle_exit(0)
        status = backend.get_status()
        assert status.state == "error"
        assert "result line" in (status.error_reason or "")


@pytest.mark.asyncio
async def test_relay_feedback_without_live_session_is_buffered_not_fatal() -> None:
    """§4 A2: no live session → the feedback rides the next dispatch."""
    backend = NareBackend()
    await backend.relay_feedback("try again")
    assert backend.get_status().state == "working"
    assert backend._session_path is None


class TestJobPayload:
    """The coordinare to nare dispatch carries provider keys by env only."""

    def _payload(self, card_context_overrides: dict[str, object]):
        card_context = {
            "id": "PVTI_1",
            "role": "implementing",
            "backend": "nare",
            "title": "t",
            "description": "d",
        }
        card_context.update(card_context_overrides)
        svc = HTTPPerformerService(
            PerformerEndpointConfig(id="e", mode="ephemeral", roles=["implementer"], image="img"),
        )
        ws = WorkspaceInfo(
            path=None, branch="nare/x", repo_url="https://github.com/o/r.git",
        )
        return svc._build_job_payload(card_context, ws)

    @staticmethod
    def _secret(payload, name: str) -> str | None:
        value = payload.secrets.get(name)
        return None if value is None else value.get_secret_value()

    def test_without_a_mode_injects_ambient_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "oai")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
        payload = self._payload({})
        assert self._secret(payload, "OPENAI_API_KEY") == "oai"
        assert self._secret(payload, "ANTHROPIC_API_KEY") == "anthropic-key"

    def test_rail_settings_never_ride_the_secrets_channel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every secret value is redacted from transcripts and logs, so a provider
        name there would erase each mention of "anthropic" from a run's record."""
        monkeypatch.setenv("MY_PROXY_KEY", "bearer-token")
        payload = self._payload({
            "endpoint_kind": "litellm", "auth_token_env": "MY_PROXY_KEY",
            "base_url": "http://litellm:4000/v1",
        })
        assert set(payload.secrets) <= {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN"}
        assert payload.metadata["endpoint_kind"] == "litellm"
        assert payload.metadata["base_url"] == "http://litellm:4000/v1"

    def test_native_anthropic_endpoint_injects_only_its_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "oai")
        monkeypatch.setenv("TEAM_ANTHROPIC_KEY", "team")
        payload = self._payload({"endpoint_kind": "anthropic", "api_key_env": "TEAM_ANTHROPIC_KEY"})
        assert self._secret(payload, "ANTHROPIC_API_KEY") == "team"
        assert "OPENAI_API_KEY" not in payload.secrets

    def test_native_openai_endpoint_injects_only_its_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
        monkeypatch.setenv("ENDPOINT_KEY", "native-key")
        payload = self._payload({"endpoint_kind": "openai", "api_key_env": "ENDPOINT_KEY"})
        assert self._secret(payload, "OPENAI_API_KEY") == "native-key"
        assert "ANTHROPIC_API_KEY" not in payload.secrets

    def test_self_hosted_endpoint_sends_its_bearer_as_the_openai_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "ambient")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
        monkeypatch.setenv("MY_PROXY_KEY", "bearer")
        payload = self._payload({
            "endpoint_kind": "litellm", "auth_token_env": "MY_PROXY_KEY",
            "base_url": "http://litellm:4000/v1",
        })
        assert self._secret(payload, "OPENAI_API_KEY") == "bearer"
        assert "ANTHROPIC_API_KEY" not in payload.secrets

    def test_keyless_self_hosted_endpoint_injects_no_ambient_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An Ollama endpoint with no auth_env must not leak the vendor keys to it."""
        monkeypatch.setenv("OPENAI_API_KEY", "ambient")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
        payload = self._payload({"endpoint_kind": "ollama", "base_url": "http://localhost:11434/v1"})
        assert "OPENAI_API_KEY" not in payload.secrets
        assert "ANTHROPIC_API_KEY" not in payload.secrets

    def test_absent_keys_stay_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        payload = self._payload({})
        assert "OPENAI_API_KEY" not in payload.secrets
        assert "ANTHROPIC_API_KEY" not in payload.secrets
