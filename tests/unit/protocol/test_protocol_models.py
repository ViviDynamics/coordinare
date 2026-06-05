from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from coordinare.protocol import ProtocolMessage, ProtocolResponse, generate_contracts


class TestProtocolMessage:
    def test_minimal_dispatch(self) -> None:
        msg = ProtocolMessage(action="dispatch")
        assert msg.action == "dispatch"
        assert msg.session_id == ""
        assert msg.payload == {}

    def test_full_dispatch(self) -> None:
        msg = ProtocolMessage(
            action="dispatch",
            session_id="s1",
            payload={"title": "Fix bug", "description": "details"},
        )
        assert msg.session_id == "s1"
        assert msg.payload["title"] == "Fix bug"

    def test_all_action_types(self) -> None:
        for action in ("dispatch", "status", "relay_feedback", "health"):
            msg = ProtocolMessage(action=action)
            assert msg.action == action

    def test_invalid_action_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProtocolMessage(action="invalid_action")

    def test_roundtrip_json(self) -> None:
        msg = ProtocolMessage(action="status", session_id="s42", payload={"key": "val"})
        raw = msg.model_dump_json()
        restored = ProtocolMessage.model_validate_json(raw)
        assert restored == msg


class TestProtocolResponse:
    def test_minimal_response(self) -> None:
        resp = ProtocolResponse(status="accepted")
        assert resp.status == "accepted"
        assert resp.session_id == ""
        assert resp.reason is None
        assert resp.questions == []
        assert resp.pr_url is None
        assert resp.pr_node_id is None
        assert resp.progress is None
        assert resp.backend is None
        assert resp.model is None

    def test_pr_opened_response(self) -> None:
        resp = ProtocolResponse(
            status="pr_opened",
            session_id="s1",
            pr_url="https://github.com/org/repo/pull/1",
            pr_node_id="PR_NODE_1",
        )
        assert resp.pr_url == "https://github.com/org/repo/pull/1"
        assert resp.pr_node_id == "PR_NODE_1"

    def test_blocked_response_with_questions(self) -> None:
        resp = ProtocolResponse(
            status="blocked",
            session_id="s1",
            questions=["What API key?", "Which env?"],
        )
        assert len(resp.questions) == 2

    def test_error_response_with_reason(self) -> None:
        resp = ProtocolResponse(
            status="error",
            session_id="s1",
            reason="Something went wrong",
        )
        assert resp.reason == "Something went wrong"

    def test_all_status_types(self) -> None:
        for status in (
            "accepted", "working", "pr_opened", "blocked", "error",
            "unknown", "busy", "acknowledged", "session_expired",
        ):
            resp = ProtocolResponse(status=status)
            assert resp.status == status

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProtocolResponse(status="invalid_status")

    def test_roundtrip_json(self) -> None:
        resp = ProtocolResponse(
            status="working",
            session_id="s1",
            progress="Building tests",
            backend="opencode",
            model="spark/qwen3.6:35b",
        )
        raw = resp.model_dump_json()
        restored = ProtocolResponse.model_validate_json(raw)
        assert restored == resp


class TestGenerateContracts:
    def test_creates_two_schema_files(self, tmp_path) -> None:
        generate_contracts(tmp_path)
        assert (tmp_path / "protocol-message.schema.json").exists()
        assert (tmp_path / "protocol-response.schema.json").exists()

    def test_output_is_valid_json(self, tmp_path) -> None:
        generate_contracts(tmp_path)
        for name in ("protocol-message.schema.json", "protocol-response.schema.json"):
            data = json.loads((tmp_path / name).read_text())
            assert isinstance(data, dict)

    def test_message_schema_has_properties(self, tmp_path) -> None:
        generate_contracts(tmp_path)
        schema = json.loads((tmp_path / "protocol-message.schema.json").read_text())
        assert "properties" in schema
        assert "action" in schema["properties"]
        assert "session_id" in schema["properties"]
        assert "payload" in schema["properties"]

    def test_response_schema_has_properties(self, tmp_path) -> None:
        generate_contracts(tmp_path)
        schema = json.loads((tmp_path / "protocol-response.schema.json").read_text())
        assert "properties" in schema
        assert "status" in schema["properties"]
        assert "session_id" in schema["properties"]
        assert "backend" in schema["properties"]
        assert "model" in schema["properties"]

    def test_creates_output_dir_if_missing(self, tmp_path) -> None:
        nested = tmp_path / "nested" / "dir"
        generate_contracts(nested)
        assert (nested / "protocol-message.schema.json").exists()

    def test_uses_expected_file_names(self, tmp_path) -> None:
        generate_contracts(tmp_path)
        files = sorted(p.name for p in tmp_path.iterdir())
        assert files == ["protocol-message.schema.json", "protocol-response.schema.json"]
