"""Unit tests for 015 config validators — auth, poll=0, webhook."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from coordinare.config import ProjectConfiguration

_BASE = {
    "project_name": "Demo",
    "github_org": "acme",
    "github_project_number": 1,
    "human_reviewers": ["alice"],
}


# ---------------------------------------------------------------------------
# PAT mode validators
# ---------------------------------------------------------------------------


def test_pat_mode_requires_token() -> None:
    with pytest.raises(ValidationError, match=r"github\.auth=pat requires github\.token"):
        ProjectConfiguration(**_BASE, github_auth="pat", github_token=None)


def test_pat_mode_empty_token_raises() -> None:
    with pytest.raises(ValidationError, match=r"github\.auth=pat requires github\.token"):
        ProjectConfiguration(**_BASE, github_auth="pat", github_token="   ")


def test_pat_mode_placeholder_token_raises() -> None:
    with pytest.raises(ValidationError, match="unresolved placeholder"):
        ProjectConfiguration(**_BASE, github_auth="pat", github_token="${GITHUB_TOKEN}")


def test_pat_mode_valid_token_passes() -> None:
    cfg = ProjectConfiguration(**_BASE, github_auth="pat", github_token="ghp_abc123")
    assert cfg.github_token.get_secret_value() == "ghp_abc123"


def test_pat_is_default_auth_mode() -> None:
    cfg = ProjectConfiguration(**_BASE, github_token="tok")
    assert cfg.github_auth == "pat"


# ---------------------------------------------------------------------------
# App mode validators
# ---------------------------------------------------------------------------


def test_app_mode_requires_all_three_fields() -> None:
    with pytest.raises(ValidationError, match=r"github\.auth=app requires"):
        ProjectConfiguration(**_BASE, github_auth="app")


def test_app_mode_missing_app_id_raises() -> None:
    with pytest.raises(ValidationError, match="github_app_id"):
        ProjectConfiguration(
            **_BASE,
            github_auth="app",
            github_private_key_path="/tmp/key.pem",
            github_installation_id=67890,
        )


def test_app_mode_missing_key_path_raises() -> None:
    with pytest.raises(ValidationError, match="github_private_key_path"):
        ProjectConfiguration(
            **_BASE,
            github_auth="app",
            github_app_id=12345,
            github_installation_id=67890,
        )


def test_app_mode_missing_installation_id_raises() -> None:
    with pytest.raises(ValidationError, match="github_installation_id"):
        ProjectConfiguration(
            **_BASE,
            github_auth="app",
            github_app_id=12345,
            github_private_key_path="/tmp/key.pem",
        )


def test_app_mode_all_fields_passes() -> None:
    cfg = ProjectConfiguration(
        **_BASE,
        github_auth="app",
        github_app_id=12345,
        github_private_key_path="/tmp/key.pem",
        github_installation_id=67890,
    )
    assert cfg.github_app_id == 12345
    assert cfg.github_installation_id == 67890


# ---------------------------------------------------------------------------
# poll=0 validators
# ---------------------------------------------------------------------------


def test_poll_zero_is_valid() -> None:
    cfg = ProjectConfiguration(**_BASE, github_token="tok", poll_interval_seconds=0)
    assert cfg.poll_interval_seconds == 0


def test_poll_zero_skips_backoff_check() -> None:
    """poll=0 must NOT raise even if resilience.wait_max_seconds > 0."""
    cfg = ProjectConfiguration(**_BASE, github_token="tok", poll_interval_seconds=0)
    # Default resilience has wait_max_seconds=30; if poll=0 guard is missing this raises.
    assert cfg.resilience.github_retry.wait_max_seconds == 30.0


def test_poll_nonzero_enforces_backoff_cap() -> None:
    from coordinare.config import ResilienceConfig, ServiceRetryConfig
    resilience = ResilienceConfig(
        github_retry=ServiceRetryConfig(wait_max_seconds=60.0),
    )
    with pytest.raises(ValidationError, match="exceeds poll_interval_seconds"):
        ProjectConfiguration(**_BASE, github_token="tok", poll_interval_seconds=30, resilience=resilience)


# ---------------------------------------------------------------------------
# Webhook validators
# ---------------------------------------------------------------------------


def test_webhook_enabled_without_secret_raises() -> None:
    from coordinare.config import WebhookConfig
    with pytest.raises(ValidationError, match=r"webhooks\.secret is required"):
        ProjectConfiguration(
            **_BASE,
            github_token="tok",
            webhooks=WebhookConfig(enabled=True, secret=None),
        )


def test_webhook_enabled_with_secret_passes() -> None:
    from coordinare.config import WebhookConfig
    cfg = ProjectConfiguration(
        **_BASE,
        github_token="tok",
        webhooks=WebhookConfig(enabled=True, secret="whsec_test"),
    )
    assert cfg.webhooks.enabled is True


def test_webhook_disabled_no_secret_passes() -> None:
    cfg = ProjectConfiguration(**_BASE, github_token="tok")
    assert cfg.webhooks.enabled is False
    assert cfg.webhooks.secret is None


def test_webhook_path_auto_adds_leading_slash() -> None:
    from coordinare.config import WebhookConfig
    cfg = WebhookConfig(path="webhook/github")
    assert cfg.path == "/webhook/github"


def test_webhook_path_empty_raises() -> None:
    from coordinare.config import WebhookConfig
    with pytest.raises(ValidationError, match="must not be empty"):
        WebhookConfig(path="")


def test_webhook_path_whitespace_raises() -> None:
    from coordinare.config import WebhookConfig
    with pytest.raises(ValidationError, match="must not contain whitespace"):
        WebhookConfig(path="/webhook github")
