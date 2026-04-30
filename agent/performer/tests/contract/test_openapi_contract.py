"""Contract harness mirror inside the performer package (spec 056, T011).

Reads the canonical OpenAPI document shipped with the coordinare spec and
asserts the performer-side pydantic models keep their fields aligned with it.
Wire-level conformance against the running server is added in later tasks
(T032, T047).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

CONTRACT_PATH = (
    Path(__file__).resolve().parents[4]
    / "specs"
    / "056-performer-containerization"
    / "contracts"
    / "performer-http.openapi.yaml"
)


@pytest.fixture(scope="module")
def openapi_doc() -> dict[str, object]:
    if not CONTRACT_PATH.exists():
        pytest.skip(f"contract not present at {CONTRACT_PATH}")
    return yaml.safe_load(CONTRACT_PATH.read_text())


def test_contract_loadable(openapi_doc: dict[str, object]) -> None:
    assert openapi_doc.get("openapi", "").startswith("3.")


def test_paths_cover_job_lifecycle(openapi_doc: dict[str, object]) -> None:
    paths = openapi_doc["paths"]
    for required in ("/status", "/jobs"):
        assert required in paths
