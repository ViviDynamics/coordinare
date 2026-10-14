from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import stamina

from coordinare.models.card import Card, CardStatus
from coordinare.services import run_header


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """218: the docker_image lane is deselected (not skipped) unless COORDINARE_DOCKER_IMAGE_TESTS is set.
    A deselected test never runs and never reports, so an unset environment cannot pass it quietly.
    With the variable set, a missing docker or image fails the module fixture."""
    if not os.environ.get("COORDINARE_DOCKER_IMAGE_TESTS"):
        kept = [item for item in items if "docker_image" not in item.keywords]
        deselected = [item for item in items if "docker_image" in item.keywords]
        if deselected:
            items[:] = kept
            config.hook.pytest_deselected(items=deselected)
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


@pytest.fixture(autouse=True)
def _persona_history_in_tmp(tmp_path, monkeypatch):
    """Spec 130: dispatch-time persona history goes to a temp dir, never the cwd.

    Tests build real configurations whose state path is relative, so the root would
    otherwise resolve into the working tree.
    """
    monkeypatch.setattr(
        run_header,
        "persona_history_root",
        lambda config: None if config is None else tmp_path / "persona_history",
    )


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
