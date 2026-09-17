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
        # 412 round 46: session_expired's process-exiting distinction rides
        # on this field, so the contract must carry it.
        assert "active_session" in schema["properties"]
        assert "backend" in schema["properties"]
        assert "model" in schema["properties"]

    def test_protocol_response_schema_has_all_status_values(self) -> None:
        schema = ProtocolResponse.model_json_schema()
        status_prop = schema["properties"]["status"]
        expected = {
            "accepted", "working", "pr_opened", "plan_committed",
            "approved", "nothing_to_review", "changes_requested",
            "security_passed", "nothing_to_scan", "not_applicable", "security_failed",
            "qa_passed", "qa_failed", "qa_env_blocked",
            "env_blocked",
            "docs_committed", "env_bootstrap_complete", "assessment_complete",
            # 410: the assessor can decline the card; terminal non-success.
            "assessment_not_work", "assessment_needs_split",
            "partial_progress",
            "blocked", "error", "unknown", "busy", "acknowledged",
            "session_expired", "token_limit", "healthy", "unhealthy",
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
        assert "active_session" in generated["properties"]
        assert "backend" in generated["properties"]
        assert "model" in generated["properties"]
        assert generated["properties"]["status"] is not None

    def test_generated_schemas_are_valid_json(self, tmp_path) -> None:
        generate_contracts(tmp_path)
        for name in ("protocol-message.schema.json", "protocol-response.schema.json"):
            data = json.loads((tmp_path / name).read_text())
            assert isinstance(data, dict)
            assert "properties" in data

    def test_payload_matches_the_action_discriminated_shapes(self, tmp_path) -> None:
        """412 round 17: status/health payloads are empty objects -- the
        contract must accept the real wire shape. 412 round 18: the shapes
        are action-discriminated (message-level if/then), so a status token
        refresh and a relay payload validate while an empty dispatch payload
        does not."""
        import jsonschema

        generate_contracts(tmp_path)
        schema = json.loads((tmp_path / "protocol-message.schema.json").read_text())
        validator = jsonschema.Draft202012Validator(schema)
        for payload, action, valid in (
            ({}, "status", True),
            ({}, "health", True),
            ({"github_token": "t"}, "status", True),
            ({"pr_url": "https://github.com/o/r/pull/1", "comments": [{"author_login": "copilot", "body": "x"}]}, "relay_feedback", True),
            ({}, "dispatch", False),
            ({"session_id": "s", "pr_url": "https://x", "reviews": []}, "relay_feedback", False),
            ({"bogus": 1}, "status", False),
        ):
            errors = list(validator.iter_errors({"action": action, "session_id": "s", "payload": payload}))
            assert (not errors) == valid, f"payload {payload} for {action}: {errors}"

    def test_response_schema_keeps_the_v1_envelope(self, tmp_path) -> None:
        """412 round 18: the generated response contract keeps the hand
        schema's identity and constraints -- draft $schema/$id, the required
        status key, and the questions minLength."""
        generate_contracts(tmp_path)
        schema = json.loads((tmp_path / "protocol-response.schema.json").read_text())
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"] == "coordinare/protocol-response/v1"
        assert schema["required"] == ["status"]
        assert schema["properties"]["questions"]["items"]["minLength"] == 1
        assert schema["properties"]["pr_url"]["anyOf"][0]["format"] == "uri"

    def test_dispatch_payload_matches_the_card_context(self, tmp_path) -> None:
        """412 round 19: the published dispatch contract describes the payload
        the caller actually emits -- _build_card_dict's id/status, not the
        phantom board_card_id/column names no code ever sent."""
        import jsonschema

        generate_contracts(tmp_path)
        schema = json.loads((tmp_path / "protocol-message.schema.json").read_text())
        validator = jsonschema.Draft202012Validator(schema)
        real = {"id": "PVT_kwDOA", "title": "t", "description": "d", "acceptance_criteria": ["a"],
                "status": "In Progress", "previous_status": "Todo", "github_token": "gh", "role": "implementer"}
        msg = {"action": "dispatch", "session_id": "s", "payload": real}
        assert list(validator.iter_errors(msg)) == []
        assert list(validator.iter_errors({**msg, "payload": {k: v for k, v in real.items() if k != "id"}})) != []

    def test_dispatch_contract_accepts_the_bootstrap_and_wiki_variants(self, tmp_path) -> None:
        """412 round 20: the dispatch contract is an anyOf of the dispatch
        variants coordinare actually sends -- an env-bootstrap job payload and
        a cardless wiki-init context validate alongside the card dispatch,
        while a payload matching no variant still fails."""
        import jsonschema

        generate_contracts(tmp_path)
        schema = json.loads((tmp_path / "protocol-message.schema.json").read_text())
        validator = jsonschema.Draft202012Validator(schema)
        bootstrap = {
            "job_type": "env_bootstrap",
            "symphony_name": "sym",
            "symphony_org": "o",
            "symphony_repo": "r",
            "env_spec_files": ["README.md"],
            "env_spec_contents": {"README.md": "x"},
            "cache_mount_path": "/devenv/sym",
            "last_failure": None,
        }
        wiki = {
            "card_id": "wiki-init-sym",
            "role": "documenting",
            "doc_mode": "init",
            "repo_url": "https://github.com/o/r.git",
            "branch": "wiki-init/sym",
            "base_branch": "main",
            "title": "Initialize the project wiki",
            "description": "d",
        }
        for payload in (bootstrap, wiki):
            msg = {"action": "dispatch", "session_id": "s", "payload": payload}
            assert list(validator.iter_errors(msg)) == []
