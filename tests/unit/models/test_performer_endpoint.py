"""Unit tests for performer endpoint validation invariants (spec 056, T005)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coordinare.models.performer_endpoint import (
    PerformerEndpointConfig,
    detect_duplicate_endpoints,
)


def _base(**overrides: object) -> dict[str, object]:
    cfg: dict[str, object] = {"id": "p1", "roles": ["performer"]}
    cfg.update(overrides)
    return cfg


class TestSubprocessMode:
    def test_default_mode_is_subprocess(self) -> None:
        cfg = PerformerEndpointConfig(**_base())
        assert cfg.mode == "subprocess"

    def test_subprocess_rejects_image(self) -> None:
        with pytest.raises(ValidationError, match="subprocess performers must not"):
            PerformerEndpointConfig(**_base(image="performer:full"))

    def test_subprocess_rejects_endpoint(self) -> None:
        with pytest.raises(ValidationError, match="subprocess performers must not"):
            PerformerEndpointConfig(**_base(endpoint="http://localhost:8080"))

    def test_subprocess_rejects_auth_token(self) -> None:
        with pytest.raises(ValidationError, match="subprocess performers must not"):
            PerformerEndpointConfig(**_base(auth_token="secret"))


class TestPersistentMode:
    def test_persistent_requires_endpoint(self) -> None:
        with pytest.raises(ValidationError, match="persistent performers require endpoint"):
            PerformerEndpointConfig(
                **_base(mode="persistent", image="performer:full"),
            )

    def test_persistent_requires_image(self) -> None:
        with pytest.raises(ValidationError, match="persistent performers require image"):
            PerformerEndpointConfig(
                **_base(mode="persistent", endpoint="http://localhost:8080"),
            )

    def test_persistent_valid(self) -> None:
        cfg = PerformerEndpointConfig(
            **_base(
                mode="persistent",
                image="performer:full",
                endpoint="http://localhost:8080",
            ),
        )
        assert cfg.mode == "persistent"
        assert str(cfg.endpoint).startswith("http://localhost:8080")


class TestEphemeralMode:
    def test_ephemeral_requires_image(self) -> None:
        with pytest.raises(ValidationError, match="ephemeral performers require image"):
            PerformerEndpointConfig(**_base(mode="ephemeral"))

    def test_ephemeral_valid_without_endpoint(self) -> None:
        cfg = PerformerEndpointConfig(
            **_base(mode="ephemeral", image="performer:slim-claude"),
        )
        assert cfg.mode == "ephemeral"
        assert cfg.endpoint is None


class TestThresholdInvariants:
    def test_failure_threshold_must_be_positive(self) -> None:
        with pytest.raises(ValidationError, match="failure_threshold must be >= 1"):
            PerformerEndpointConfig(**_base(failure_threshold=0))

    def test_failure_threshold_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PerformerEndpointConfig(**_base(failure_threshold=-1))

    def test_readiness_timeout_must_be_positive(self) -> None:
        with pytest.raises(ValidationError, match="readiness_timeout_s must be >= 1"):
            PerformerEndpointConfig(**_base(readiness_timeout_s=0))

    def test_readiness_timeout_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PerformerEndpointConfig(**_base(readiness_timeout_s=-5))


class TestExtraFieldsForbidden:
    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PerformerEndpointConfig(**_base(unknown_field="x"))


class TestDetectDuplicateEndpoints:
    def test_no_duplicates_returns_empty(self) -> None:
        configs = [
            PerformerEndpointConfig(
                **_base(id="p1", mode="persistent", image="i", endpoint="http://a:8080"),
            ),
            PerformerEndpointConfig(
                **_base(id="p2", mode="persistent", image="i", endpoint="http://b:8080"),
            ),
        ]
        assert detect_duplicate_endpoints(configs) == []

    def test_duplicate_endpoints_detected(self) -> None:
        configs = [
            PerformerEndpointConfig(
                **_base(id="p1", mode="persistent", image="i", endpoint="http://a:8080"),
            ),
            PerformerEndpointConfig(
                **_base(id="p2", mode="persistent", image="i", endpoint="http://a:8080"),
            ),
        ]
        dups = detect_duplicate_endpoints(configs)
        assert len(dups) == 1
        assert "a:8080" in dups[0]

    def test_subprocess_configs_skipped(self) -> None:
        configs = [
            PerformerEndpointConfig(**_base(id="p1")),
            PerformerEndpointConfig(**_base(id="p2")),
        ]
        assert detect_duplicate_endpoints(configs) == []


class TestFieldValidators:
    def test_readiness_timeout_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match="readiness_timeout_s"):
            PerformerEndpointConfig(**_base(
                mode="ephemeral", image="img", readiness_timeout_s=0,
            ))

    def test_failure_threshold_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match="failure_threshold"):
            PerformerEndpointConfig(**_base(
                mode="ephemeral", image="img", failure_threshold=0,
            ))

    def test_subprocess_rejects_port(self) -> None:
        with pytest.raises(ValidationError, match="subprocess performers must not"):
            PerformerEndpointConfig(**_base(port=8080))

    def test_subprocess_rejects_volumes(self) -> None:
        with pytest.raises(ValidationError, match="subprocess performers must not"):
            PerformerEndpointConfig(**_base(volumes=[{"host_path": "/tmp", "container_path": "/w"}]))

    def test_subprocess_rejects_secret_sources(self) -> None:
        with pytest.raises(ValidationError, match="subprocess performers must not"):
            PerformerEndpointConfig(**_base(secret_sources={"init_payload": False}))

    def test_subprocess_rejects_egress_allowlist(self) -> None:
        with pytest.raises(ValidationError, match="subprocess performers must not"):
            PerformerEndpointConfig(**_base(egress_allowlist=["api.github.com"]))

    def test_ephemeral_accepts_egress_allowlist(self) -> None:
        cfg = PerformerEndpointConfig(**_base(
            mode="ephemeral",
            image="img",
            egress_allowlist=["api.github.com", "objects.githubusercontent.com"],
        ))
        assert cfg.egress_allowlist == ["api.github.com", "objects.githubusercontent.com"]


class TestJobStatusProgressValidator:
    def test_progress_pct_out_of_range_rejected(self) -> None:
        from coordinare.models.performer_endpoint import JobStatus

        with pytest.raises(ValidationError, match="progress_pct"):
            JobStatus(
                job_id="j1",
                state="running",
                started_at="2026-01-01T00:00:00Z",
                progress_pct=150,
            )

    def test_progress_pct_none_allowed(self) -> None:
        from coordinare.models.performer_endpoint import JobStatus

        s = JobStatus(
            job_id="j1",
            state="running",
            started_at="2026-01-01T00:00:00Z",
            progress_pct=None,
        )
        assert s.progress_pct is None

    def test_progress_pct_in_range_accepted(self) -> None:
        from coordinare.models.performer_endpoint import JobStatus

        s = JobStatus(
            job_id="j1",
            state="running",
            started_at="2026-01-01T00:00:00Z",
            progress_pct=42,
        )
        assert s.progress_pct == 42


class TestPositiveDefaultsAccepted:
    def test_readiness_timeout_positive_accepted(self) -> None:
        cfg = PerformerEndpointConfig(**_base(readiness_timeout_s=30))
        assert cfg.readiness_timeout_s == 30

    def test_failure_threshold_positive_accepted(self) -> None:
        cfg = PerformerEndpointConfig(**_base(failure_threshold=5))
        assert cfg.failure_threshold == 5

    def test_subprocess_rejects_env(self) -> None:
        with pytest.raises(ValidationError, match="subprocess performers must not"):
            PerformerEndpointConfig(**_base(env={"FOO": "bar"}))

    def test_subprocess_rejects_container_devenv_root(self) -> None:
        with pytest.raises(ValidationError, match="subprocess performers must not"):
            PerformerEndpointConfig(**_base(container_devenv_root="/other"))


class TestEnvCoercion:
    """YAML ${VAR} expansion can yield bare ints/bools/None; coerce to str."""

    def _ephemeral(self, env: object) -> PerformerEndpointConfig:
        return PerformerEndpointConfig(
            id="p1",
            mode="ephemeral",
            roles=["performer"],
            image="performer:full",
            env=env,  # type: ignore[arg-type]
        )

    def test_int_value_coerced(self) -> None:
        cfg = self._ephemeral({"MAX_TOOL_CALLS": 200})
        assert cfg.env == {"MAX_TOOL_CALLS": "200"}

    def test_bool_value_coerced_lowercase(self) -> None:
        cfg = self._ephemeral({"DEBUG": True, "QUIET": False})
        assert cfg.env == {"DEBUG": "true", "QUIET": "false"}

    def test_none_value_becomes_empty_string(self) -> None:
        cfg = self._ephemeral({"UNSET": None})
        assert cfg.env == {"UNSET": ""}

    def test_float_value_coerced(self) -> None:
        cfg = self._ephemeral({"TEMP": 0.5})
        assert cfg.env == {"TEMP": "0.5"}

    def test_string_value_unchanged(self) -> None:
        cfg = self._ephemeral({"NAME": "alice"})
        assert cfg.env == {"NAME": "alice"}
