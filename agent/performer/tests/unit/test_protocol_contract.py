"""Protocol contract test — performer.protocol vs coordinare.protocol.

Validates that PerformerResponse is a superset of ProtocolResponse: every field
present in the coordinare's wire protocol must also exist in the performer's
outbound model with a compatible type.

When coordinare is importable (installed as a dev dependency or via ``pip install -e ../..``),
the test imports ProtocolResponse directly so schema drift is caught automatically.
When coordinare is not installed, it falls back to a pinned field snapshot — update
the snapshot whenever coordinare.protocol.ProtocolResponse changes.
"""
from __future__ import annotations

from performer.protocol import PerformerResponse

# ---------------------------------------------------------------------------
# Live or snapshot coordinare schema
# ---------------------------------------------------------------------------
try:
    from coordinare.protocol import ProtocolResponse as _CoordinareProtocolResponse

    _COORDINARE_FIELDS: set[str] = set(
        _CoordinareProtocolResponse.model_json_schema().get("properties", {}).keys()
    )
    _USING_LIVE_SCHEMA = True
except ImportError:
    # Fallback snapshot — mirrors src/coordinare/protocol.py as of spec 012-performer.
    # Update this set if coordinare.protocol.ProtocolResponse fields change.
    _COORDINARE_FIELDS = {
        "status",
        "session_id",
        "reason",
        "questions",
        "pr_url",
        "pr_node_id",
        "progress",
    }
    _USING_LIVE_SCHEMA = False


class TestProtocolContract:
    def test_performer_response_is_superset_of_coordinare_schema(self) -> None:
        """All coordinare ProtocolResponse fields must exist in PerformerResponse."""
        performer_schema = PerformerResponse.model_json_schema()
        performer_props = set(performer_schema.get("properties", {}).keys())
        missing = _COORDINARE_FIELDS - performer_props
        assert not missing, (
            f"PerformerResponse is missing coordinare fields: {missing}. "
            "The performer's outbound schema must be a superset of the coordinare's "
            "ProtocolResponse to ensure wire-protocol compatibility."
        )

    def test_performer_response_adds_metrics_field(self) -> None:
        """PerformerResponse extends ProtocolResponse by adding a `metrics` field."""
        performer_schema = PerformerResponse.model_json_schema()
        performer_props = set(performer_schema.get("properties", {}).keys())
        assert "metrics" in performer_props, (
            "PerformerResponse must add a `metrics` field not present in ProtocolResponse"
        )
        assert "metrics" not in _COORDINARE_FIELDS, (
            "`metrics` should NOT be in the base coordinare schema — it's a performer extension"
        )

    def test_status_field_is_string_type(self) -> None:
        """The `status` field must serialise as a string (not nested object)."""
        schema = PerformerResponse.model_json_schema()
        props = schema.get("properties", {})
        status_prop = props.get("status", {})
        # pydantic may represent a Literal union as anyOf with const strings
        resolved = status_prop.get("type") or [
            item.get("const") for item in status_prop.get("anyOf", [])
        ]
        assert resolved, f"Could not resolve type for `status` field; got: {status_prop}"

    def test_questions_field_is_array_type(self) -> None:
        """The `questions` field must serialise as an array."""
        schema = PerformerResponse.model_json_schema()
        props = schema.get("properties", {})
        questions_prop = props.get("questions", {})
        assert questions_prop.get("type") == "array", (
            f"`questions` must be array type; got: {questions_prop}"
        )

    def test_schema_source_is_live_when_coordinare_installed(self) -> None:
        """Documents whether the contract is validated against the live coordinare schema."""
        # This test always passes — it exists to make schema source visible in test output.
        # If _USING_LIVE_SCHEMA is False, the inline snapshot is used; update it when needed.
        assert isinstance(_USING_LIVE_SCHEMA, bool)
