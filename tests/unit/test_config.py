from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from coordinare.config import (
    BranchCollisionStrategy,
    PerformerRoleConfig,
    PerformersConfig,
    PersonaConfig,
    PersonasConfig,
    PriorityConfig,
    ProjectConfiguration,
)


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


def test_dashboard_port_default(tmp_path) -> None:
    config = ProjectConfiguration.from_yaml(_write_config(tmp_path))
    assert config.dashboard_port == 8090


def test_dashboard_host_default(tmp_path) -> None:
    config = ProjectConfiguration.from_yaml(_write_config(tmp_path))
    assert config.dashboard_host == "127.0.0.1"


def test_dashboard_port_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("COORDINARE_DASHBOARD_PORT", "9000")
    config = ProjectConfiguration.from_yaml(_write_config(tmp_path))
    assert config.dashboard_port == 9000


def test_dashboard_host_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("COORDINARE_DASHBOARD_HOST", "0.0.0.0")
    config = ProjectConfiguration.from_yaml(_write_config(tmp_path))
    assert config.dashboard_host == "0.0.0.0"


def test_env_var_overrides_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "token-from-env")
    config = ProjectConfiguration.from_yaml(_write_config(tmp_path, github_token="token-from-file"))
    assert config.github_token.get_secret_value() == "token-from-env"


@pytest.mark.parametrize("value", [-1, 3601])
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

    async def _noop_run(config, config_path=None):
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


# --- T030: performer_image field (012-performer) ---


def test_performer_image_default(tmp_path) -> None:
    config = ProjectConfiguration.from_yaml(_write_config(tmp_path))
    assert config.performer_image == "coordinare-performer:full"


def test_performer_image_yaml_override(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "\n".join([
            'project_name: "Demo"',
            'github_org: "acme"',
            "github_project_number: 12",
            'github_token: "tok"',
            'human_reviewers: ["alice"]',
            'performer_image: "coordinare-performer:custom"',
        ])
    )
    config = ProjectConfiguration.from_yaml(path)
    assert config.performer_image == "coordinare-performer:custom"


def test_performer_image_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("COORDINARE_PERFORMER_IMAGE", "myregistry/performer:v2")
    config = ProjectConfiguration.from_yaml(_write_config(tmp_path))
    assert config.performer_image == "myregistry/performer:v2"


def test_app_auth_with_all_required_fields_passes_validation(tmp_path) -> None:
    """config.py line 364->378: github_auth='app' with all required fields passes (returns self)."""

    key_file = tmp_path / "key.pem"
    key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\n")

    config = ProjectConfiguration(
        project_name="Demo",
        github_org="acme",
        github_project_number=1,
        github_auth="app",
        github_app_id=12345,
        github_private_key_path=key_file,
        github_installation_id=67890,
        human_reviewers=["alice"],
    )
    assert config.github_auth == "app"
    assert config.github_app_id == 12345


def test_app_auth_with_missing_fields_raises_validation_error() -> None:
    """config.py line 364->378: github_auth='app' with missing fields raises ValueError."""
    import pytest

    with pytest.raises(Exception, match="github_app_id"):
        ProjectConfiguration(
            project_name="Demo",
            github_org="acme",
            github_project_number=1,
            github_auth="app",
            # github_app_id missing → should raise
            human_reviewers=["alice"],
        )


def test_channel_config_subject_template_none_passes_validation() -> None:
    """config.py line 76: _validate_template_syntax returns v when v is None."""
    from pydantic import SecretStr

    from coordinare.config import ChannelConfig
    from coordinare.models.notification import ChannelType

    cfg = ChannelConfig(
        name="slack-alerts",
        type=ChannelType.slack,
        webhook_url=SecretStr("https://hooks.slack.com/T1"),
        subject_template=None,
    )
    assert cfg.subject_template is None


def test_backoff_cap_skipped_when_poll_interval_is_zero() -> None:
    """_validate_retry_backoff_caps returns early when poll_interval_seconds=0,
    so a very large wait_max_seconds does NOT raise a ValidationError."""
    from coordinare.config import ResilienceConfig, ServiceRetryConfig

    # poll_interval_seconds=0 disables the cap check entirely; even a massive
    # wait_max_seconds value should be accepted without error.
    config = ProjectConfiguration(
        project_name="Demo",
        github_org="acme",
        github_project_number=1,
        github_token="tok",
        human_reviewers=["alice"],
        poll_interval_seconds=0,
        resilience=ResilienceConfig(
            github_retry=ServiceRetryConfig(
                attempts=3,
                wait_initial_seconds=1.0,
                wait_max_seconds=300.0,
                wait_jitter_seconds=1.0,
            ),
        ),
    )
    assert config.poll_interval_seconds == 0


# ---------------------------------------------------------------------------
# WebhooksConfig — whitespace in path (line 33->35)
# ---------------------------------------------------------------------------


def test_webhook_path_with_whitespace_raises() -> None:
    """Line 30->32: path containing whitespace raises ValueError."""
    import pytest

    from coordinare.config import WebhookConfig

    with pytest.raises(ValueError, match="whitespace"):
        WebhookConfig(path="/web hooks")


def test_webhook_path_without_leading_slash_gets_prepended() -> None:
    """Line 33->34: path without leading slash → slash is prepended."""
    from coordinare.config import WebhookConfig

    cfg = WebhookConfig(path="webhook/github")
    assert cfg.path == "/webhook/github"


def test_webhook_path_with_leading_slash_unchanged() -> None:
    """Line 33->35 (False branch): path already starts with '/' → no prepend."""
    from coordinare.config import WebhookConfig

    cfg = WebhookConfig(path="/my-webhook")
    assert cfg.path == "/my-webhook"


# ---------------------------------------------------------------------------
# ProjectConfiguration.from_yaml — non-existent file (line 325->330)
# and YAML that loads as non-dict (line 328->330)
# ---------------------------------------------------------------------------


def test_from_yaml_with_nonexistent_file_uses_defaults(tmp_path, monkeypatch) -> None:
    """Line 325->330: config_path.exists() is False → raw stays {} → env/defaults used."""
    monkeypatch.setenv("COORDINARE_PROJECT_NAME", "Demo")
    monkeypatch.setenv("COORDINARE_GITHUB_ORG", "acme")
    monkeypatch.setenv("COORDINARE_GITHUB_PROJECT_NUMBER", "1")
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "tok")
    monkeypatch.setenv("COORDINARE_HUMAN_REVIEWERS", '["alice"]')
    path = tmp_path / "does_not_exist.yaml"
    config = ProjectConfiguration.from_yaml(path)
    assert config is not None


def test_from_yaml_with_non_dict_yaml_uses_defaults(tmp_path, monkeypatch) -> None:
    """Line 328->330: loaded YAML is not a dict (e.g. a list) → raw stays {} → env used."""
    monkeypatch.setenv("COORDINARE_PROJECT_NAME", "Demo")
    monkeypatch.setenv("COORDINARE_GITHUB_ORG", "acme")
    monkeypatch.setenv("COORDINARE_GITHUB_PROJECT_NUMBER", "1")
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "tok")
    monkeypatch.setenv("COORDINARE_HUMAN_REVIEWERS", '["alice"]')
    path = tmp_path / "list.yaml"
    path.write_text("- item1\n- item2\n")
    config = ProjectConfiguration.from_yaml(path)
    assert config is not None


# ---------------------------------------------------------------------------
# 018 — PersonaConfig and PersonasConfig model tests (T005)
# ---------------------------------------------------------------------------


def test_personas_config_parses_all_eight_roles_from_yaml(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.dump({
            "project_name": "Demo",
            "github_org": "acme",
            "github_project_number": 1,
            "github_token": "tok",
            "human_reviewers": ["alice"],
            "personas": {
                "advocate": {"instructions": "scan issues"},
                "assessor": {"instructions": "assess cards"},
                "architect": {"instructions": "plan work"},
                "implementer": {"instructions": "write code"},
                "reviewer": {"instructions": "review pr"},
                "security": {"instructions": "check security"},
                "qa": {"instructions": "validate tests"},
                "tech_writer": {"instructions": "write docs"},
            },
        })
    )
    config = ProjectConfiguration.from_yaml(path)
    assert config.personas.advocate.instructions == "scan issues"
    assert config.personas.assessor.instructions == "assess cards"
    assert config.personas.architect.instructions == "plan work"
    assert config.personas.implementer.instructions == "write code"
    assert config.personas.reviewer.instructions == "review pr"
    assert config.personas.security.instructions == "check security"
    assert config.personas.qa.instructions == "validate tests"
    assert config.personas.tech_writer.instructions == "write docs"


def test_missing_personas_key_produces_all_default_personas_config(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.dump({
            "project_name": "Demo",
            "github_org": "acme",
            "github_project_number": 1,
            "github_token": "tok",
            "human_reviewers": ["alice"],
        })
    )
    config = ProjectConfiguration.from_yaml(path)
    assert isinstance(config.personas, PersonasConfig)
    assert config.personas.implementer.instructions == ""


def test_empty_instructions_field_parsed_correctly() -> None:
    pc = PersonaConfig(instructions="")
    assert pc.instructions == ""


def test_role_isolation_sc004() -> None:
    """SC-004: changing one role's persona must not affect others."""
    personas = PersonasConfig()
    personas.implementer.instructions = "custom"  # type: ignore[misc]
    assert personas.assessor.instructions == ""


def test_persona_config_rejects_instructions_exceeding_max_length() -> None:
    """T020: config load rejects instructions > 8000 chars with ValidationError."""
    from coordinare.config import PERSONA_MAX_LENGTH
    with pytest.raises(ValidationError):
        PersonaConfig(instructions="x" * (PERSONA_MAX_LENGTH + 1))


def test_project_configuration_raises_validation_error_for_oversized_persona(tmp_path) -> None:
    """T020: ProjectConfiguration.from_yaml raises ValidationError when YAML contains oversized persona."""
    from coordinare.config import PERSONA_MAX_LENGTH
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.dump({
            "project_name": "Demo",
            "github_org": "acme",
            "github_project_number": 1,
            "github_token": "tok",
            "human_reviewers": ["alice"],
            "personas": {
                "implementer": {"instructions": "x" * (PERSONA_MAX_LENGTH + 1)},
            },
        })
    )
    with pytest.raises(ValidationError):
        ProjectConfiguration.from_yaml(path)


# ---------------------------------------------------------------------------
# 019 — Performer Lifecycle config models
# ---------------------------------------------------------------------------


class TestPerformerRoleConfig:
    """Tests for PerformerRoleConfig model."""

    def test_defaults(self) -> None:
        cfg = PerformerRoleConfig()
        assert cfg.backend == "opencode"
        assert cfg.transport is None  # None = falls back to global config default
        assert cfg.image is None
        assert cfg.executable is None
        assert cfg.host is None
        assert cfg.port is None
        assert cfg.timeout_seconds is None

    def test_custom_values(self) -> None:
        cfg = PerformerRoleConfig(
            backend="claude-code",
            transport="kubernetes",
            image="coordinare-performer:latest",
            executable="/usr/bin/claude",
            host="k8s.internal",
            port=8080,
            timeout_seconds=3600,
        )
        assert cfg.backend == "claude-code"
        assert cfg.transport == "kubernetes"
        assert cfg.image == "coordinare-performer:latest"
        assert cfg.timeout_seconds == 3600


class TestPerformersConfig:
    """Tests for PerformersConfig model."""

    def test_all_roles_default_to_none(self) -> None:
        cfg = PerformersConfig()
        for role in ("advocate", "assessor", "architect", "implementer",
                     "reviewer", "security", "qa", "tech_writer"):
            assert getattr(cfg, role) is None

    def test_two_roles_configured(self) -> None:
        cfg = PerformersConfig(
            implementer=PerformerRoleConfig(backend="opencode"),
            reviewer=PerformerRoleConfig(backend="claude-code"),
        )
        assert cfg.implementer is not None
        assert cfg.implementer.backend == "opencode"
        assert cfg.reviewer is not None
        assert cfg.reviewer.backend == "claude-code"
        assert cfg.architect is None

    def test_invalid_role_field_type_raises(self) -> None:
        with pytest.raises(ValidationError):
            PerformersConfig(implementer="not-a-config")  # type: ignore[arg-type]


class TestPerformersInProjectConfiguration:
    """Tests for performers field on ProjectConfiguration."""

    def test_missing_performers_key_loads_default(self, tmp_path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({
            "project_name": "Demo",
            "github_org": "acme",
            "github_project_number": 1,
            "github_token": "tok",
            "human_reviewers": ["alice"],
        }))
        cfg = ProjectConfiguration.from_yaml(path)
        assert isinstance(cfg.performers, PerformersConfig)
        assert cfg.performers.implementer is None

    def test_performers_with_roles_configured(self, tmp_path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({
            "project_name": "Demo",
            "github_org": "acme",
            "github_project_number": 1,
            "github_token": "tok",
            "human_reviewers": ["alice"],
            "performers": {
                "implementer": {"backend": "opencode", "transport": "subprocess"},
                "security": {"backend": "claude-code", "transport": "kubernetes", "image": "sec:v1"},
            },
        }))
        cfg = ProjectConfiguration.from_yaml(path)
        assert cfg.performers.implementer is not None
        assert cfg.performers.implementer.backend == "opencode"
        assert cfg.performers.security is not None
        assert cfg.performers.security.image == "sec:v1"
        assert cfg.performers.reviewer is None


# ---------------------------------------------------------------------------
# 025 — PriorityConfig tests
# ---------------------------------------------------------------------------


class TestPriorityConfig:
    def test_defaults(self) -> None:
        cfg = PriorityConfig()
        assert cfg.field_name is None
        assert cfg.priority_order == []

    def test_with_field_name(self) -> None:
        cfg = PriorityConfig(field_name="Priority")
        assert cfg.field_name == "Priority"

    def test_with_priority_order(self) -> None:
        cfg = PriorityConfig(field_name="P", priority_order=["P0", "P1", "P2"])
        assert cfg.priority_order == ["P0", "P1", "P2"]

    def test_missing_priority_key_in_yaml(self, tmp_path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({
            "project_name": "Demo", "github_org": "acme",
            "github_project_number": 1, "github_token": "tok",
            "human_reviewers": ["alice"],
        }))
        cfg = ProjectConfiguration.from_yaml(path)
        assert cfg.priority.field_name is None
        assert cfg.priority.priority_order == []

    def test_priority_in_yaml(self, tmp_path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({
            "project_name": "Demo", "github_org": "acme",
            "github_project_number": 1, "github_token": "tok",
            "human_reviewers": ["alice"],
            "priority": {"field_name": "Urgency", "priority_order": ["Critical", "High"]},
        }))
        cfg = ProjectConfiguration.from_yaml(path)
        assert cfg.priority.field_name == "Urgency"
        assert cfg.priority.priority_order == ["Critical", "High"]


# ---------------------------------------------------------------------------
# 036 — GitHub Enterprise Support: configurable API URLs
# ---------------------------------------------------------------------------


class TestGitHubEnterpriseURLs:
    """Tests for github_api_url and github_graphql_url config fields (036)."""

    def test_default_api_url(self, tmp_path) -> None:
        cfg = ProjectConfiguration.from_yaml(_write_config(tmp_path))
        assert cfg.github_api_url == "https://api.github.com"

    def test_default_graphql_url(self, tmp_path) -> None:
        cfg = ProjectConfiguration.from_yaml(_write_config(tmp_path))
        assert cfg.github_graphql_url == "https://api.github.com/graphql"

    def test_custom_api_url(self, tmp_path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({
            "project_name": "Demo", "github_org": "acme",
            "github_project_number": 1, "github_token": "tok",
            "human_reviewers": ["alice"],
            "github_api_url": "https://github.acme.corp/api/v3",
        }))
        cfg = ProjectConfiguration.from_yaml(path)
        assert cfg.github_api_url == "https://github.acme.corp/api/v3"

    def test_custom_graphql_url(self, tmp_path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({
            "project_name": "Demo", "github_org": "acme",
            "github_project_number": 1, "github_token": "tok",
            "human_reviewers": ["alice"],
            "github_graphql_url": "https://github.acme.corp/api/graphql",
        }))
        cfg = ProjectConfiguration.from_yaml(path)
        assert cfg.github_graphql_url == "https://github.acme.corp/api/graphql"

    def test_trailing_slash_stripped(self) -> None:
        cfg = ProjectConfiguration(
            project_name="Demo", github_org="acme",
            github_project_number=1, github_token="tok",
            human_reviewers=["alice"],
            github_api_url="https://github.acme.corp/api/v3/",
            github_graphql_url="https://github.acme.corp/api/graphql/",
        )
        assert cfg.github_api_url == "https://github.acme.corp/api/v3"
        assert cfg.github_graphql_url == "https://github.acme.corp/api/graphql"

    def test_invalid_scheme_rejected(self) -> None:
        with pytest.raises(ValidationError, match="http or https"):
            ProjectConfiguration(
                project_name="Demo", github_org="acme",
                github_project_number=1, github_token="tok",
                human_reviewers=["alice"],
                github_api_url="ftp://github.acme.corp/api/v3",
            )

    def test_http_scheme_accepted(self) -> None:
        """http is allowed for local dev environments."""
        cfg = ProjectConfiguration(
            project_name="Demo", github_org="acme",
            github_project_number=1, github_token="tok",
            human_reviewers=["alice"],
            github_api_url="http://localhost:8080/api/v3",
        )
        assert cfg.github_api_url == "http://localhost:8080/api/v3"

    def test_env_var_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("COORDINARE_GITHUB_API_URL", "https://ghes.internal/api/v3")
        cfg = ProjectConfiguration.from_yaml(_write_config(tmp_path))
        assert cfg.github_api_url == "https://ghes.internal/api/v3"

    def test_env_var_override_graphql(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("COORDINARE_GITHUB_GRAPHQL_URL", "https://ghes.internal/api/graphql")
        cfg = ProjectConfiguration.from_yaml(_write_config(tmp_path))
        assert cfg.github_graphql_url == "https://ghes.internal/api/graphql"

    def test_missing_netloc_rejected(self) -> None:
        """URLs like 'https:///api/v3' (no host) must be rejected."""
        with pytest.raises(ValidationError, match="must include a host"):
            ProjectConfiguration(
                project_name="Demo", github_org="acme",
                github_project_number=1, github_token="tok",
                human_reviewers=["alice"],
                github_api_url="https:///api/v3",
            )

    def test_whitespace_in_url_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not contain whitespace"):
            ProjectConfiguration(
                project_name="Demo", github_org="acme",
                github_project_number=1, github_token="tok",
                human_reviewers=["alice"],
                github_api_url="https://github .com/api/v3",
            )

    def test_credentials_in_url_rejected(self) -> None:
        """URLs with embedded credentials are rejected."""
        with pytest.raises(ValidationError, match="credentials"):
            ProjectConfiguration(
                project_name="Demo", github_org="acme",
                github_project_number=1, github_token="tok",
                human_reviewers=["alice"],
                github_api_url="https://user:pass@ghes.example.com/api/v3",
            )

    def test_query_string_in_url_rejected(self) -> None:
        """URLs with query parameters are rejected."""
        with pytest.raises(ValidationError, match="query"):
            ProjectConfiguration(
                project_name="Demo", github_org="acme",
                github_project_number=1, github_token="tok",
                human_reviewers=["alice"],
                github_api_url="https://ghes.example.com/api/v3?debug=1",
            )

    def test_http_non_localhost_rejected(self) -> None:
        """http scheme on non-localhost host is rejected."""
        with pytest.raises(ValidationError, match="localhost"):
            ProjectConfiguration(
                project_name="Demo", github_org="acme",
                github_project_number=1, github_token="tok",
                human_reviewers=["alice"],
                github_api_url="http://ghes.example.com/api/v3",
            )


# --- 050: assignee_filter tests ---

def test_assignee_filter_default_is_none(tmp_path) -> None:
    config = ProjectConfiguration.from_yaml(_write_config(tmp_path))
    assert config.assignee_filter is None


def test_assignee_filter_parsed_from_yaml(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "\n".join([
            'project_name: "Demo"',
            'github_org: "acme"',
            "github_project_number: 12",
            'github_token: "tok"',
            'agent_executable: "/usr/bin/agent"',
            'human_reviewers: ["alice"]',
            'assignee_filter: "coordinare-bot"',
        ])
    )
    config = ProjectConfiguration.from_yaml(path)
    assert config.assignee_filter == "coordinare-bot"


def test_assignee_filter_env_var_override(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("COORDINARE_ASSIGNEE_FILTER", "my-bot")
    config = ProjectConfiguration.from_yaml(_write_config(tmp_path))
    assert config.assignee_filter == "my-bot"


# --- 051: bot_identity and env_passthrough tests ---

def test_bot_identity_default_name(tmp_path) -> None:
    config = ProjectConfiguration.from_yaml(_write_config(tmp_path))
    assert config.bot_identity.name == "Coordinare Bot"


def test_bot_identity_default_email(tmp_path) -> None:
    config = ProjectConfiguration.from_yaml(_write_config(tmp_path))
    assert config.bot_identity.email == "coordinare@localhost"


def test_bot_identity_parsed_from_yaml(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "\n".join([
            'project_name: "Demo"',
            'github_org: "acme"',
            "github_project_number: 12",
            'github_token: "tok"',
            'agent_executable: "/usr/bin/agent"',
            'human_reviewers: ["alice"]',
            "bot_identity:",
            '  name: "my-bot"',
            '  email: "my-bot@company.com"',
        ])
    )
    config = ProjectConfiguration.from_yaml(path)
    assert config.bot_identity.name == "my-bot"
    assert config.bot_identity.email == "my-bot@company.com"


def test_env_passthrough_parsed_from_yaml(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "\n".join([
            'project_name: "Demo"',
            'github_org: "acme"',
            "github_project_number: 12",
            'github_token: "tok"',
            'agent_executable: "/usr/bin/agent"',
            'human_reviewers: ["alice"]',
            "env_passthrough:",
            "  - ANTHROPIC_API_KEY",
            "  - OPENCODE_MODEL",
        ])
    )
    config = ProjectConfiguration.from_yaml(path)
    assert config.env_passthrough == ["ANTHROPIC_API_KEY", "OPENCODE_MODEL"]


# ---------------------------------------------------------------------------
# 052 — Stale Branch Cleanup config fields
# ---------------------------------------------------------------------------


class TestStaleBranchCleanupConfig:
    """T002: Unit tests for stale_branch_cleanup and branch_collision_strategy fields."""

    def _base_cfg(self, **extra):
        return ProjectConfiguration(
            project_name="Demo",
            github_org="acme",
            github_project_number=1,
            github_token="tok",
            human_reviewers=["alice"],
            **extra,
        )

    def test_defaults_parse_correctly(self) -> None:
        cfg = self._base_cfg()
        assert cfg.stale_branch_cleanup is True
        assert cfg.branch_collision_strategy == BranchCollisionStrategy.delete
        assert cfg.max_closed_pr_attempts_per_issue == 0

    def test_stale_branch_cleanup_false_disables_feature(self) -> None:
        cfg = self._base_cfg(stale_branch_cleanup=False)
        assert cfg.stale_branch_cleanup is False

    def test_branch_collision_strategy_suffix_parses_as_enum(self) -> None:
        cfg = self._base_cfg(branch_collision_strategy="suffix")
        assert cfg.branch_collision_strategy == BranchCollisionStrategy.suffix

    def test_closed_pr_attempt_limit_parses(self) -> None:
        cfg = self._base_cfg(max_closed_pr_attempts_per_issue=3)
        assert cfg.max_closed_pr_attempts_per_issue == 3

    def test_env_var_override_disables_cleanup(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("COORDINARE_STALE_BRANCH_CLEANUP", "false")
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({
            "project_name": "Demo",
            "github_org": "acme",
            "github_project_number": 1,
            "github_token": "tok",
            "human_reviewers": ["alice"],
        }))
        cfg = ProjectConfiguration.from_yaml(path)
        assert cfg.stale_branch_cleanup is False
