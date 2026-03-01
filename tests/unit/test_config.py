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
                'agent_executable: "/usr/local/bin/agent"',
                'human_reviewers: ["alice"]',
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
            human_reviewers=["alice"],
            poll_interval_seconds=value,
        )


def test_requires_human_reviewer() -> None:
    with pytest.raises(ValidationError):
        ProjectConfiguration(
            project_name="Demo",
            github_org="acme",
            github_project_number=1,
            github_token="tok",
            human_reviewers=[],
        )


def test_requires_non_empty_token() -> None:
    with pytest.raises(ValidationError):
        ProjectConfiguration(
            project_name="Demo",
            github_org="acme",
            github_project_number=1,
            github_token="   ",
            human_reviewers=["alice"],
        )


def test_rejects_unresolved_github_token_placeholder() -> None:
    with pytest.raises(ValidationError, match="unresolved placeholder"):
        ProjectConfiguration(
            project_name="Demo",
            github_org="acme",
            github_project_number=1,
            github_token="${COORDINARE_GITHUB_TOKEN}",
            human_reviewers=["alice"],
        )


# --- T040: state_file_path env var override ---


def test_state_file_path_env_var_override(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """COORDINARE_STATE_FILE_PATH env var overrides default state_file_path (FR-010)."""
    from pathlib import Path

    monkeypatch.setenv("COORDINARE_STATE_FILE_PATH", "/tmp/custom.json")
    config = ProjectConfiguration.from_yaml(_write_config(tmp_path))
    assert config.state_file_path == Path("/tmp/custom.json")


def test_backoff_cap_rejects_wait_max_exceeding_poll_interval() -> None:
    """FR-003: wait_max_seconds must not exceed poll_interval_seconds."""
    from coordinare.config import ResilienceConfig, ServiceRetryConfig

    with pytest.raises(ValidationError, match="wait_max_seconds"):
        ProjectConfiguration(
            project_name="Demo",
            github_org="acme",
            github_project_number=1,
            github_token="tok",
            human_reviewers=["alice"],
            poll_interval_seconds=10,
            resilience=ResilienceConfig(
                github_retry=ServiceRetryConfig(
                    attempts=3, wait_initial_seconds=1.0,
                    wait_max_seconds=60.0, wait_jitter_seconds=1.0,
                ),
            ),
        )


def test_backoff_cap_accepts_valid_config() -> None:
    """FR-003: wait_max_seconds <= poll_interval_seconds passes validation."""
    config = ProjectConfiguration(
        project_name="Demo",
        github_org="acme",
        github_project_number=1,
        github_token="tok",
        human_reviewers=["alice"],
        poll_interval_seconds=30,
    )
    assert config.resilience.github_retry.wait_max_seconds <= config.poll_interval_seconds


def test_state_file_path_default(tmp_path) -> None:
    """Default state_file_path is ./coordinare.state.json."""
    from pathlib import Path

    config = ProjectConfiguration.from_yaml(_write_config(tmp_path))
    assert config.state_file_path == Path("./coordinare.state.json")


# --- T030: AdvocateConfig validation ---


def test_advocate_enabled_with_empty_github_repo_raises() -> None:
    """enabled=True with empty github_repo → raises ValueError (T030)."""
    from coordinare.config import AdvocateConfig

    with pytest.raises(ValidationError):
        AdvocateConfig(enabled=True, github_repo="")


def test_advocate_confidence_threshold_zero_raises() -> None:
    """confidence_threshold must be > 0.0 (T030)."""
    from coordinare.config import AdvocateConfig

    with pytest.raises(ValidationError):
        AdvocateConfig(confidence_threshold=0.0, github_repo="repo")


def test_advocate_confidence_threshold_above_one_raises() -> None:
    """confidence_threshold must be <= 1.0 (T030)."""
    from coordinare.config import AdvocateConfig

    with pytest.raises(ValidationError):
        AdvocateConfig(confidence_threshold=1.1, github_repo="repo")


def test_advocate_default_sensitive_keywords_match_spec() -> None:
    """Default sensitive_keywords list contains FR-007 entries (T030)."""
    from coordinare.config import AdvocateConfig

    config = AdvocateConfig()
    keywords = [k.lower() for k in config.sensitive_keywords]
    for expected in ["billing", "payment", "legal", "security", "gdpr", "refund"]:
        assert expected in keywords, f"missing FR-007 keyword: {expected}"


def test_advocate_disabled_by_default() -> None:
    """Advocate is disabled by default."""
    from coordinare.config import AdvocateConfig

    config = AdvocateConfig()
    assert config.enabled is False


def test_advocate_enabled_with_valid_repo() -> None:
    """enabled=True with non-empty github_repo passes validation."""
    from coordinare.config import AdvocateConfig

    config = AdvocateConfig(enabled=True, github_repo="coordinare")
    assert config.enabled is True
    assert config.github_repo == "coordinare"


def test_advocate_doc_branch_defaults_to_head() -> None:
    """doc_branch defaults to 'HEAD' (repo default branch)."""
    from coordinare.config import AdvocateConfig

    config = AdvocateConfig()
    assert config.doc_branch == "HEAD"


def test_advocate_doc_branch_configurable() -> None:
    """doc_branch can be overridden to any ref string."""
    from coordinare.config import AdvocateConfig

    config = AdvocateConfig(doc_branch="stable")
    assert config.doc_branch == "stable"


# --- T025: SC-006 structured startup log ---


def test_daemon_startup_emits_config_loaded_log(tmp_path) -> None:
    """SC-006: config_loaded structured log emitted at daemon startup with required fields (T025)."""
    from unittest.mock import patch

    import structlog.testing

    from coordinare.__main__ import main

    config_file = _write_config(tmp_path)

    async def _noop_run(config):
        pass

    with (
        patch("sys.argv", ["coordinare", "--config", str(config_file)]),
        patch("coordinare.__main__._run", new=_noop_run),
        patch("coordinare.__main__.configure_logging"),
        structlog.testing.capture_logs() as cap_logs,
    ):
        main()

    config_loaded = [e for e in cap_logs if e.get("event") == "config_loaded"]
    assert config_loaded, (
        f"config_loaded event not found; captured events: {[e.get('event') for e in cap_logs]}"
    )
    event = config_loaded[0]
    assert isinstance(event["config_file"], str) and event["config_file"], (
        "config_file must be a non-empty string"
    )
    assert isinstance(event["env_var_fields_count"], int), "env_var_fields_count must be an int"
    assert isinstance(event["deprecated_fields_detected"], bool), (
        "deprecated_fields_detected must be a bool"
    )
