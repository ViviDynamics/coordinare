"""Real-image CI must exercise accepted identities rather than hold before Docker."""
from __future__ import annotations

import json

from coordinare_service_inference.environment.models import ExecutionCapabilities

from coordinare.services.environment_acceptance import accepted_environment
from tests.integration import reviewed_environment as fixture


def test_image_fixture_binds_actual_pinned_image_and_platform(tmp_path, monkeypatch):
    image = "trusted/image@sha256:" + "a" * 64
    monkeypatch.setenv("COORDINARE_E2E_PERFORMER_IMAGE", image)
    monkeypatch.setattr(fixture, "_docker", lambda *args, **kwargs: json.dumps([{"Os": "linux", "Architecture": "arm64"}]))
    manifest = fixture.manifest_for("suite", files={"guide": "documented baseline"},
        checks=[{"id": "baseline", "argv": ["true"]}])
    expected = ExecutionCapabilities(schema_version=1, image_ref=image, platform="linux/aarch64",
        cache_mount_path="/devenv/suite", install_network="enabled", verify_network="disabled")
    assert manifest.capabilities_digest == expected.digest()
    state = fixture.accepted_state(tmp_path, manifest, {"guide": "documented baseline"})
    cache = state["env_cache"]["suite"]
    assert accepted_environment(cache) is not None
    assert state["config"].performer_image == image
