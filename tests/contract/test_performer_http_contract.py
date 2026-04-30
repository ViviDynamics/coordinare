"""Contract harness for the performer HTTP job protocol (spec 056, T010).

Validates that the published OpenAPI spec is well-formed and that the pydantic
models in ``coordinare.models.performer_endpoint`` round-trip the schemas
declared in ``performer-http.openapi.yaml``. Wire-level conformance is added by
later tasks (T030, T046) once the coordinare-side HTTP transport is wired.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "specs"
    / "056-performer-containerization"
    / "contracts"
    / "performer-http.openapi.yaml"
)


@pytest.fixture(scope="module")
def openapi_doc() -> dict[str, object]:
    return yaml.safe_load(CONTRACT_PATH.read_text())


def test_openapi_document_validates(openapi_doc: dict[str, object]) -> None:
    openapi_spec_validator = pytest.importorskip("openapi_spec_validator")
    openapi_spec_validator.validate(openapi_doc)


def test_required_paths_present(openapi_doc: dict[str, object]) -> None:
    paths = openapi_doc["paths"]
    for required in ("/status", "/jobs"):
        assert required in paths, f"missing path: {required}"


def test_required_schemas_present(openapi_doc: dict[str, object]) -> None:
    schemas = openapi_doc["components"]["schemas"]
    for required in (
        "PerformerStatus",
        "JobInitPayload",
        "JobAcceptResponse",
        "JobBusyResponse",
        "JobStatus",
    ):
        assert required in schemas, f"missing schema: {required}"


def test_pydantic_models_align_with_contract(openapi_doc: dict[str, object]) -> None:
    """Every PerformerStatus required field has a matching pydantic field."""
    from coordinare.models.performer_endpoint import PerformerStatus

    contract_schema = openapi_doc["components"]["schemas"]["PerformerStatus"]
    contract_required = set(contract_schema.get("required", []))
    pydantic_fields = set(PerformerStatus.model_fields.keys())
    missing = contract_required - pydantic_fields
    assert not missing, f"PerformerStatus pydantic missing required fields: {missing}"
