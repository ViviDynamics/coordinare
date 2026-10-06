"""Unit tests for #526 — performer image digest pinning at dispatch time."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from coordinare.config import ProjectConfiguration
from coordinare.services.image_digest import (
    DigestResolutionError,
    PerformerImageResolver,
    parse_image_ref,
)
from coordinare.services.performer_lifecycle import LifecycleError


def _inspect_runner(digest: str, calls: list[tuple[str, ...]]):
    async def runner(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        calls.append(tuple(args))
        return (0, digest, "")

    return runner


def _failing_runner(calls: list[tuple[str, ...]]):
    async def runner(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        calls.append(tuple(args))
        return (1, "", "no such host")

    return runner


class TestParseImageRef:
    def test_plain_tag(self) -> None:
        assert parse_image_ref("coordinare-performer:full") == (
            "coordinare-performer",
            "full",
        )

    def test_registry_host_tag(self) -> None:
        assert parse_image_ref("ghcr.io/vividynamics/coordinare-performer:extra") == (
            "ghcr.io/vividynamics/coordinare-performer",
            "extra",
        )

    def test_missing_tag_defaults_to_latest(self) -> None:
        assert parse_image_ref("coordinare-performer") == (
            "coordinare-performer",
            "latest",
        )

    def test_registry_host_port_and_tag(self) -> None:
        assert parse_image_ref("reg.local:5000/coordinare-performer:v1") == (
            "reg.local:5000/coordinare-performer",
            "v1",
        )


class TestResolveDigest:
    @pytest.mark.asyncio
    async def test_disabled_returns_image_unchanged(self) -> None:
        calls: list[tuple[str, ...]] = []
        resolver = PerformerImageResolver(
            enabled=False, runner=_inspect_runner("sha256:abc", calls),
        )
        assert await resolver.pin("coordinare-performer:full") == "coordinare-performer:full"
        assert calls == []

    @pytest.mark.asyncio
    async def test_digest_form_passes_through(self) -> None:
        calls: list[tuple[str, ...]] = []
        resolver = PerformerImageResolver(
            enabled=True, runner=_inspect_runner("sha256:abc", calls),
        )
        image = "coordinare-performer@sha256:deadbeef"
        assert await resolver.pin(image) == image
        assert calls == []

    @pytest.mark.asyncio
    async def test_enabled_resolves_to_digest_pinned_ref(self) -> None:
        calls: list[tuple[str, ...]] = []
        resolver = PerformerImageResolver(
            enabled=True, runner=_inspect_runner("sha256:abc123", calls),
        )
        pinned = await resolver.pin("coordinare-performer:full")
        assert pinned == "coordinare-performer@sha256:abc123"
        assert calls == [
            ("buildx", "imagetools", "inspect",
             "coordinare-performer:full", "--format", "{{.Manifest.Digest}}"),
        ]

    @pytest.mark.asyncio
    async def test_cache_hit_skips_second_resolution(self) -> None:
        calls: list[tuple[str, ...]] = []
        resolver = PerformerImageResolver(
            enabled=True, runner=_inspect_runner("sha256:abc123", calls),
        )
        first = await resolver.pin("coordinare-performer:full")
        second = await resolver.pin("coordinare-performer:full")
        assert first == second == "coordinare-performer@sha256:abc123"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_cache_expires_and_re_resolves(self) -> None:
        calls: list[tuple[str, ...]] = []
        clock = {"now": 1000.0}
        resolver = PerformerImageResolver(
            enabled=True,
            refresh_seconds=60,
            runner=_inspect_runner("sha256:abc123", calls),
            clock=lambda: clock["now"],
        )
        await resolver.pin("coordinare-performer:full")
        clock["now"] = 1001.0
        await resolver.pin("coordinare-performer:full")
        assert len(calls) == 1
        clock["now"] = 1000.0 + 61.0
        await resolver.pin("coordinare-performer:full")
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_repushed_tag_refreshes_to_new_digest(self) -> None:
        digests = iter(["sha256:old", "sha256:new"])
        calls: list[tuple[str, ...]] = []
        clock = {"now": 1000.0}
        runner = _failing_inspect_with_digests(digests, calls, clock)
        resolver = PerformerImageResolver(
            enabled=True, refresh_seconds=60, runner=runner, clock=lambda: clock["now"],
        )
        assert await resolver.pin("coordinare-performer:full") == (
            "coordinare-performer@sha256:old"
        )
        clock["now"] = 2000.0
        assert await resolver.pin("coordinare-performer:full") == (
            "coordinare-performer@sha256:new"
        )

    @pytest.mark.asyncio
    async def test_resolution_failure_fails_loud(self) -> None:
        calls: list[tuple[str, ...]] = []
        resolver = PerformerImageResolver(
            enabled=True, runner=_failing_runner(calls),
        )
        with pytest.raises(DigestResolutionError) as exc_info:
            await resolver.pin("coordinare-performer:full")
        assert isinstance(exc_info.value, LifecycleError)
        assert "coordinare-performer:full" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_failure_does_not_poison_cache(self) -> None:
        failing_calls: list[tuple[str, ...]] = []
        ok_calls: list[tuple[str, ...]] = []
        resolver = PerformerImageResolver(
            enabled=True, runner=_failing_runner(failing_calls),
        )
        with pytest.raises(DigestResolutionError):
            await resolver.pin("coordinare-performer:full")
        ok_runner = _inspect_runner("sha256:abc123", ok_calls)
        resolver._runner = ok_runner
        assert await resolver.pin("coordinare-performer:full") == (
            "coordinare-performer@sha256:abc123"
        )


def _failing_inspect_with_digests(
    digests: Any, calls: list[tuple[str, ...]], clock: dict[str, float],
) -> Any:
    async def runner(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        calls.append(tuple(args))
        return (0, next(digests), "")

    return runner


class TestAsyncPin:
    @pytest.mark.asyncio
    async def test_async_pin_matches_sync(self) -> None:
        calls: list[tuple[str, ...]] = []
        resolver = PerformerImageResolver(
            enabled=True, runner=_inspect_runner("sha256:abc123", calls),
        )
        pinned = await resolver.pin("coordinare-performer:full")
        assert pinned == "coordinare-performer@sha256:abc123"

    @pytest.mark.asyncio
    async def test_concurrent_pins_share_one_resolution(self) -> None:
        calls: list[tuple[str, ...]] = []
        resolver = PerformerImageResolver(
            enabled=True, runner=_inspect_runner("sha256:abc123", calls),
        )
        results = await asyncio.gather(
            *[
                resolver.pin("coordinare-performer:full"),
                resolver.pin("coordinare-performer:full"),
                resolver.pin("coordinare-performer:full"),
            ],
        )
        assert len(set(results)) == 1
        assert len(calls) == 1


class TestConfig:
    def _config(self, **overrides) -> ProjectConfiguration:
        from coordinare.config import PerformerRoleConfig, PerformersConfig

        return ProjectConfiguration(
            project_name="test-project",
            github_org="acme",
            github_project_number=1,
            github_token="token",
            human_reviewers=["alice"],
            performers=PerformersConfig(
                default=PerformerRoleConfig(backend="codex", effort="high"),
            ),
            **overrides,
        )

    def test_digest_pin_defaults_off(self) -> None:
        config = self._config()
        assert config.performer_digest_pin.enabled is False
        assert config.performer_digest_pin.refresh_seconds == 300

    def test_digest_pin_enabled(self) -> None:
        from coordinare.config import PerformerDigestPinConfig

        config = self._config(
            performer_digest_pin=PerformerDigestPinConfig(
                enabled=True, refresh_seconds=60,
            ),
        )
        assert config.performer_digest_pin.enabled is True
        assert config.performer_digest_pin.refresh_seconds == 60


class TestRuntimeWiring:
    @pytest.mark.asyncio
    async def test_docker_runtime_pins_image_before_container_start(self) -> None:
        from coordinare.services.docker_runtime import DockerRuntime

        resolver = PerformerImageResolver(
            enabled=True, runner=_inspect_runner("sha256:abc123", []),
        )
        runtime = DockerRuntime(image_resolver=resolver)
        config = _ephemeral_config()
        captured: dict[str, Any] = {}

        def fake_start_ephemeral(cfg, **kwargs):
            captured["image"] = cfg.image
            return _fake_started()

        with patch.object(
            _lifecycle_module(), "start_ephemeral", side_effect=fake_start_ephemeral,
        ):
            started = await runtime.start_ephemeral(config, backend="driver")
        assert captured["image"] == "coordinare-performer@sha256:abc123"
        assert started.endpoint.startswith("http://")

    @pytest.mark.asyncio
    async def test_docker_runtime_without_resolver_uses_image_verbatim(self) -> None:
        from coordinare.services.docker_runtime import DockerRuntime

        runtime = DockerRuntime()
        config = _ephemeral_config()
        captured: dict[str, Any] = {}

        def fake_start_ephemeral(cfg, **kwargs):
            captured["image"] = cfg.image
            return _fake_started()

        with patch.object(
            _lifecycle_module(), "start_ephemeral", side_effect=fake_start_ephemeral,
        ):
            await runtime.start_ephemeral(config)
        assert captured["image"] == "coordinare-performer:full"


def _ephemeral_config():
    from coordinare.models.performer_endpoint import PerformerEndpointConfig

    return PerformerEndpointConfig.model_validate(
        {
            "id": "worker",
            "roles": ["worker"],
            "image": "coordinare-performer:full",
            "mode": "ephemeral",
        },
    )


def _fake_started():
    from coordinare.services.performer_lifecycle import StartedContainer

    return StartedContainer(
        container_id="container-1", endpoint="http://127.0.0.1:8088",
    )


def _lifecycle_module():
    from coordinare.services import performer_lifecycle

    return performer_lifecycle


class TestKubernetesManifest:
    def test_pod_manifest_uses_pinned_image(self) -> None:
        from coordinare.services.kubernetes_runtime import build_pod_manifest

        config = _ephemeral_config()
        config = config.model_copy(update={"image": "coordinare-performer@sha256:abc123"})
        manifest = build_pod_manifest(config, pod_name="p", performer_id="worker")
        assert manifest["spec"]["containers"][0]["image"] == (
            "coordinare-performer@sha256:abc123"
        )

    @pytest.mark.asyncio
    async def test_kubernetes_runtime_pins_image_before_manifest(self) -> None:
        from coordinare.services import kubernetes_runtime as k8s_mod
        from coordinare.services.kubernetes_runtime import KubernetesRuntime

        calls: list[tuple[str, ...]] = []
        resolver = PerformerImageResolver(
            enabled=True, runner=_inspect_runner("sha256:abc123", calls),
        )
        runtime = KubernetesRuntime(
            core_v1=_FakeK8sApi(captured_manifest := {}), image_resolver=resolver,
        )
        captured: dict[str, Any] = {}

        real_manifest = k8s_mod.build_pod_manifest

        def spy(config, **kwargs):
            manifest = real_manifest(config, **kwargs)
            captured["image"] = config.image
            return manifest

        with patch.object(k8s_mod, "build_pod_manifest", side_effect=spy):
            await runtime.start_ephemeral(_ephemeral_config(), backend="driver")
        assert captured["image"] == "coordinare-performer@sha256:abc123"
        assert captured_manifest.get("image") == "coordinare-performer@sha256:abc123"


class _FakeK8sApi:
    def __init__(self, captured: dict[str, Any]) -> None:
        self.captured = captured

    def create_namespaced_pod(self, namespace=None, body=None, **kwargs):
        self.captured["image"] = body["spec"]["containers"][0]["image"]
        return

    def read_namespaced_pod(self, name=None, namespace=None, **kwargs):
        import types

        return types.SimpleNamespace(
            status=types.SimpleNamespace(
                phase="Running", pod_ip="10.0.0.1", reason="", container_statuses=[],
            ),
        )

    def delete_namespaced_pod(self, *args, **kwargs):
        return None
