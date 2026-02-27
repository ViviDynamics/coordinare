from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import stamina

from coordinare.models.card import Card, CardStatus


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
