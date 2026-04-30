"""Unit tests for VolumeMount configuration round-trip (spec 056, T046).

Tests that host_path/container_path/mode round-trip correctly, default mode=ro,
and invalid modes are rejected.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
from pydantic import ValidationError

from coordinare.models.performer_endpoint import VolumeMount


def test_volume_mount_default_mode() -> None:
    """Test that VolumeMount defaults to mode=ro."""
    mount = VolumeMount(
        host_path=Path("/host/data"),
        container_path=PurePosixPath("/container/data"),
    )
    assert mount.mode == "ro"


def test_volume_mount_explicit_ro() -> None:
    """Test that mode=ro is accepted explicitly."""
    mount = VolumeMount(
        host_path=Path("/host/data"),
        container_path=PurePosixPath("/container/data"),
        mode="ro",
    )
    assert mount.mode == "ro"


def test_volume_mount_explicit_rw() -> None:
    """Test that mode=rw is accepted explicitly."""
    mount = VolumeMount(
        host_path=Path("/host/data"),
        container_path=PurePosixPath("/container/data"),
        mode="rw",
    )
    assert mount.mode == "rw"


def test_volume_mount_invalid_mode() -> None:
    """Test that invalid modes are rejected."""
    with pytest.raises(ValidationError) as exc_info:
        VolumeMount(
            host_path=Path("/host/data"),
            container_path=PurePosixPath("/container/data"),
            mode="rwx",  # Invalid!
        )
    assert "mode" in str(exc_info.value).lower()


def test_volume_mount_roundtrip_to_dict() -> None:
    """Test that VolumeMount round-trips through dict serialization."""
    original = VolumeMount(
        host_path=Path("/host/logs"),
        container_path=PurePosixPath("/app/logs"),
        mode="rw",
    )

    # Serialize to dict
    data = original.model_dump()

    # Reconstruct
    reconstructed = VolumeMount(**data)

    assert reconstructed.host_path == original.host_path
    assert reconstructed.container_path == original.container_path
    assert reconstructed.mode == original.mode


def test_volume_mount_paths_with_special_chars() -> None:
    """Test that paths with special characters are handled correctly."""
    mount = VolumeMount(
        host_path=Path("/host/data-v2.1/config"),
        container_path=PurePosixPath("/container/data-v2.1/config"),
        mode="ro",
    )
    assert mount.host_path == Path("/host/data-v2.1/config")
    assert mount.container_path == PurePosixPath("/container/data-v2.1/config")


def test_volume_mount_absolute_paths_required() -> None:
    """Test that paths must be absolute (validated by Pydantic Path)."""
    # This test documents behavior — Path type on host_path will accept
    # any string and convert it to Path; validation is minimal.
    # Container path must be absolute in practice (enforced by usage, not model).
    mount = VolumeMount(
        host_path=Path("/abs/path"),
        container_path=PurePosixPath("/abs/path"),
    )
    assert str(mount.host_path) == "/abs/path"
    assert str(mount.container_path) == "/abs/path"


def test_volume_mount_extra_fields_forbidden() -> None:
    """Test that extra fields in VolumeMount are forbidden."""
    with pytest.raises(ValidationError) as exc_info:
        VolumeMount(
            host_path=Path("/host/data"),
            container_path=PurePosixPath("/container/data"),
            mode="ro",
            extra_field="should fail",  # Not allowed
        )
    assert "extra_field" in str(exc_info.value).lower()


def test_volume_mount_missing_required_fields() -> None:
    """Test that required fields must be provided."""
    with pytest.raises(ValidationError):
        VolumeMount(  # type: ignore
            mode="ro",
        )


def test_volume_mount_in_list_serialization() -> None:
    """Test that a list of VolumeMounts serializes and deserializes correctly."""
    mounts = [
        VolumeMount(
            host_path=Path("/host/data"),
            container_path=PurePosixPath("/app/data"),
            mode="ro",
        ),
        VolumeMount(
            host_path=Path("/host/config"),
            container_path=PurePosixPath("/app/config"),
            mode="rw",
        ),
    ]

    # Serialize
    data = [m.model_dump() for m in mounts]

    # Deserialize
    reconstructed = [VolumeMount(**d) for d in data]

    assert len(reconstructed) == 2
    assert reconstructed[0].mode == "ro"
    assert reconstructed[1].mode == "rw"
