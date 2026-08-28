from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import stamina

from coordinare.models.card import Card, CardStatus


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """151: skip `real`-marked tests unless RUN_REAL_BENCH is set (opt-in paid/
    Docker lane; the free deterministic lane must stay green — SC-006)."""
    if os.environ.get("RUN_REAL_BENCH"):
        return
    skip_real = pytest.mark.skip(reason="opt-in real lane; set RUN_REAL_BENCH=1 to run")
    for item in items:
        if "real" in item.keywords:
            item.add_marker(skip_real)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


@pytest.fixture(scope="session")
def docker_available() -> bool:
    return _docker_available()


@pytest.fixture
def require_docker(docker_available: bool) -> None:
    if not docker_available:
        pytest.skip("Docker daemon not reachable; skipping containerized test")


@pytest.fixture(autouse=True)
def _disable_stamina_retries():
    stamina.set_active(False)
    yield
    stamina.set_active(True)


@pytest.fixture
def mock_gql_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_ssh_connection() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_smtp_server() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_slack_webhook() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def sample_card() -> Card:
    now = datetime.now(UTC)
    return Card(
        id="PVI_1",
        issue_id="I_1",
        issue_number=42,
        title="Implement API endpoint",
        description="Add endpoint for health checks",
        status=CardStatus.TODO,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def sample_review() -> dict[str, object]:
    return {
        "id": "RVW_1",
        "author_login": "reviewer1",
        "state": "COMMENTED",
        "body": "Please add tests.",
    }
