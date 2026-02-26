from __future__ import annotations

import pytest
from pydantic import ValidationError

from coordinare.config import ProjectConfiguration


def _write_config(tmp_path, github_token: str = "token-from-file"):
    path = tmp_path / "config.yaml"
    path.write_text(
        "\n".join(
            [
                'project_name: "Demo"',
                'github_org: "acme"',
                "github_project_number: 12",
                f'github_token: "{github_token}"',
                'agent_host: "agent.example.com"',
                'agent_user: "coordinare"',
                'agent_command: "agent run --card-context {card_context}"',
                'human_reviewers: ["alice"]',
                'notification_email: "team@example.com"',
                'smtp_host: "smtp.example.com"',
                'slack_webhook_url: "https://hooks.slack.com/services/T/B/C"',
                'slack_channel: "#eng"',
            ]
        )
    )
    return path


def test_loads_yaml_config(tmp_path) -> None:
    config = ProjectConfiguration.from_yaml(_write_config(tmp_path))
    assert config.github_org == "acme"
    assert config.poll_interval_seconds == 30


def test_env_var_overrides_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "token-from-env")
    config = ProjectConfiguration.from_yaml(_write_config(tmp_path, github_token="token-from-file"))
    assert config.github_token.get_secret_value() == "token-from-env"


@pytest.mark.parametrize("value", [9, 301])
def test_poll_interval_bounds(value: int) -> None:
    with pytest.raises(ValidationError):
        ProjectConfiguration(
            project_name="Demo",
            github_org="acme",
            github_project_number=1,
            github_token="tok",
            agent_host="agent",
            agent_user="user",
            agent_command="agent {card_context}",
            human_reviewers=["alice"],
            smtp_host="smtp",
            slack_webhook_url="https://hooks.slack.com/services/T/B/C",
            slack_channel="#eng",
            poll_interval_seconds=value,
        )


def test_requires_human_reviewer() -> None:
    with pytest.raises(ValidationError):
        ProjectConfiguration(
            project_name="Demo",
            github_org="acme",
            github_project_number=1,
            github_token="tok",
            agent_host="agent",
            agent_user="user",
            agent_command="agent {card_context}",
            human_reviewers=[],
            smtp_host="smtp",
            slack_webhook_url="https://hooks.slack.com/services/T/B/C",
            slack_channel="#eng",
        )


def test_requires_non_empty_token() -> None:
    with pytest.raises(ValidationError):
        ProjectConfiguration(
            project_name="Demo",
            github_org="acme",
            github_project_number=1,
            github_token="   ",
            agent_host="agent",
            agent_user="user",
            agent_command="agent {card_context}",
            human_reviewers=["alice"],
            smtp_host="smtp",
            slack_webhook_url="https://hooks.slack.com/services/T/B/C",
            slack_channel="#eng",
        )


def test_requires_agent_placeholder() -> None:
    with pytest.raises(ValidationError):
        ProjectConfiguration(
            project_name="Demo",
            github_org="acme",
            github_project_number=1,
            github_token="tok",
            agent_host="agent",
            agent_user="user",
            agent_command="agent dispatch",
            human_reviewers=["alice"],
            smtp_host="smtp",
            slack_webhook_url="https://hooks.slack.com/services/T/B/C",
            slack_channel="#eng",
        )


def test_rejects_unresolved_github_token_placeholder() -> None:
    with pytest.raises(ValidationError, match="unresolved placeholder"):
        ProjectConfiguration(
            project_name="Demo",
            github_org="acme",
            github_project_number=1,
            github_token="${COORDINARE_GITHUB_TOKEN}",
            agent_host="agent",
            agent_user="user",
            agent_command="agent {card_context}",
            human_reviewers=["alice"],
            smtp_host="smtp",
            slack_webhook_url="https://hooks.slack.com/services/T/B/C",
            slack_channel="#eng",
        )


def test_rejects_unresolved_slack_webhook_placeholder() -> None:
    with pytest.raises(ValidationError, match="unresolved placeholder"):
        ProjectConfiguration(
            project_name="Demo",
            github_org="acme",
            github_project_number=1,
            github_token="tok",
            agent_host="agent",
            agent_user="user",
            agent_command="agent {card_context}",
            human_reviewers=["alice"],
            smtp_host="smtp",
            slack_webhook_url="${COORDINARE_SLACK_WEBHOOK_URL}",
            slack_channel="#eng",
        )


def test_rejects_empty_slack_webhook_url() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        ProjectConfiguration(
            project_name="Demo",
            github_org="acme",
            github_project_number=1,
            github_token="tok",
            agent_host="agent",
            agent_user="user",
            agent_command="agent {card_context}",
            human_reviewers=["alice"],
            smtp_host="smtp",
            slack_webhook_url="   ",
            slack_channel="#eng",
        )


# --- T040: state_file_path env var override ---


def test_state_file_path_env_var_override(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """COORDINARE_STATE_FILE_PATH env var overrides default state_file_path (FR-010)."""
    from pathlib import Path

    monkeypatch.setenv("COORDINARE_STATE_FILE_PATH", "/tmp/custom.json")
    config = ProjectConfiguration.from_yaml(_write_config(tmp_path))
    assert config.state_file_path == Path("/tmp/custom.json")


def test_state_file_path_default(tmp_path) -> None:
    """Default state_file_path is ./coordinare.state.json."""
    from pathlib import Path

    config = ProjectConfiguration.from_yaml(_write_config(tmp_path))
    assert config.state_file_path == Path("./coordinare.state.json")
