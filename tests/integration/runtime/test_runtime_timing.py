from __future__ import annotations

from coordinare.config import ProjectConfiguration


def test_default_heartbeat_meets_30s_requirement() -> None:
    config = ProjectConfiguration(
        project_name="Demo",
        github_org="acme",
        github_project_number=1,
        github_token="tok",
        human_reviewers=["alice"],
    )

    assert config.heartbeat_interval_seconds <= 30
