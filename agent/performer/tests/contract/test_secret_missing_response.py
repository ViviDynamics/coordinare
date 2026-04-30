"""Contract test for secret-missing 422 response (T056).

Spec requirement: POST /jobs for a job whose required secret is unavailable
returns 422 with JobBusyResponse.reason="secret_missing" and a detail
referencing the secret name.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from performer.server.job_runner import JobRunner
from performer.server.models import (
    JobInitPayload,
    JobResult,
    PerformerCapabilities,
)
from performer.server.routes import register_routes


@pytest.fixture
def app_with_secret_failure() -> FastAPI:
    """FastAPI app configured to fail on missing secrets."""
    from performer.server.secrets import SecretResolver, SecretSourceConfig

    app = FastAPI()

    # Mock executor that checks for required secrets
    async def executor(payload: JobInitPayload) -> JobResult:
        # This payload will trigger secret resolution in the runner
        # For this test, we rely on the resolver failing
        return JobResult(success=True, summary="test")

    # Disable env + creds_file so the resolver always raises SecretMissingError.
    # env=True would silently resolve if GITHUB_TOKEN is set in the CI environment.
    resolver = SecretResolver(
        SecretSourceConfig(init_payload=True, env=False, creds_file=False),
        init_secrets={},
        creds_file=None,
    )

    # Wire resolver into the runner
    runner = JobRunner(executor, resolver=resolver)

    app.state.runner = runner
    app.state.capabilities = PerformerCapabilities(
        backends=["claude_code"],
        tool_flags=["git"],
    )
    app.state.auth_enabled = False
    app.state.version = "1.0.0"

    register_routes(app)
    return app


@pytest.fixture
def client(app_with_secret_failure: FastAPI) -> TestClient:
    """Test client for the app."""
    return TestClient(app_with_secret_failure)


class TestSecretMissingResponse:
    """422 response when required secret is missing."""

    def test_secret_missing_returns_422(self, client: TestClient) -> None:
        """POST /jobs returns 422 when required secret is unavailable."""
        payload = {
            "job_id": "job-123",
            "card_id": "card-456",
            "role": "write",
            "backend": "claude_code",
            "persona": "test",
            "repo_url": "https://github.com/test/repo",
            "branch": "main",
            "secrets": {},  # No required secret provided
            "metadata": {},
        }

        response = client.post("/jobs", json=payload)

        assert response.status_code == 422

    def test_secret_missing_response_has_reason(self, client: TestClient) -> None:
        """Response includes reason=secret_missing and detail naming the secret."""
        payload = {
            "job_id": "job-123",
            "card_id": "card-456",
            "role": "write",
            "backend": "claude_code",
            "persona": "test",
            "repo_url": "https://github.com/test/repo",
            "branch": "main",
            "secrets": {},
            "metadata": {},
        }

        response = client.post("/jobs", json=payload)

        assert response.status_code == 422
        data = response.json()
        assert data["reason"] == "secret_missing"
        assert data["detail"] == "GITHUB_TOKEN"
