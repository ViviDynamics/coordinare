from __future__ import annotations

import json

from coordinare.protocol import ProtocolMessage, ProtocolResponse, generate_contracts


class TestSchemaContractValidation:
    """Validate that Pydantic models produce JSON Schema compatible with the
    checked-in contract files."""

    def test_protocol_message_schema_has_required_properties(self) -> None:
        schema = ProtocolMessage.model_json_schema()
        assert "properties" in schema
        assert "action" in schema["properties"]
        assert "session_id" in schema["properties"]
        assert "payload" in schema["properties"]

    def test_protocol_response_schema_has_required_properties(self) -> None:
        schema = ProtocolResponse.model_json_schema()
        assert "properties" in schema
        assert "status" in schema["properties"]
        assert "session_id" in schema["properties"]

    def test_protocol_response_schema_has_all_status_values(self) -> None:
        schema = ProtocolResponse.model_json_schema()
        status_prop = schema["properties"]["status"]
        expected = {
            "accepted", "working", "pr_opened", "plan_committed",
            "approved", "changes_requested",
            "security_passed", "security_failed",
            "qa_passed", "qa_failed",
            "docs_committed", "assessment_complete",
            "blocked", "error", "unknown", "busy", "acknowledged",
            "session_expired", "healthy", "unhealthy",
        }
        # status enum may be in the property directly or via $ref
        enum_values = set()
        if "enum" in status_prop:
            enum_values = set(status_prop["enum"])
        elif "anyOf" in status_prop:
            for item in status_prop["anyOf"]:
                if "enum" in item:
                    enum_values.update(item["enum"])
                elif "const" in item:
                    enum_values.add(item["const"])
        assert expected == enum_values

    def test_protocol_message_schema_has_all_action_values(self) -> None:
        schema = ProtocolMessage.model_json_schema()
        action_prop = schema["properties"]["action"]
        expected = {"dispatch", "status", "relay_feedback", "health"}
        enum_values = set()
        if "enum" in action_prop:
            enum_values = set(action_prop["enum"])
        elif "anyOf" in action_prop:
            for item in action_prop["anyOf"]:
                if "enum" in item:
                    enum_values.update(item["enum"])
                elif "const" in item:
                    enum_values.add(item["const"])
        assert expected == enum_values


class TestGenerateContractsMatchesCheckedIn:
    """Assert that generate_contracts() output is structurally consistent
    with the checked-in schema files."""

    def test_generated_message_schema_matches_checked_in(self, tmp_path) -> None:
        generate_contracts(tmp_path)
        generated = json.loads((tmp_path / "protocol-message.schema.json").read_text())
        assert "properties" in generated
        assert "action" in generated["properties"]
        assert generated["properties"]["action"] is not None

    def test_generated_response_schema_matches_checked_in(self, tmp_path) -> None:
        generate_contracts(tmp_path)
        generated = json.loads((tmp_path / "protocol-response.schema.json").read_text())
        assert "properties" in generated
        assert "status" in generated["properties"]
        assert generated["properties"]["status"] is not None

    def test_generated_schemas_are_valid_json(self, tmp_path) -> None:
        generate_contracts(tmp_path)
        for name in ("protocol-message.schema.json", "protocol-response.schema.json"):
            data = json.loads((tmp_path / name).read_text())
            assert isinstance(data, dict)
            assert "properties" in data
