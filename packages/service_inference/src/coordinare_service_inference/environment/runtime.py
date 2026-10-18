"""Explicit optional platform policy, requested endpoints and sealed artifacts.

These contracts describe operator-supplied facilities. They contain no project
recipes, discovery heuristics, local credentials or readiness exemptions.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SHA256 = r'^[0-9a-f]{64}$'
IMAGE = r'^[^\s@]+@sha256:[0-9a-f]{64}$'
LINUX_PLATFORM = r'^linux/(?:x86_64|aarch64)$'


class RuntimeContract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)

    @model_validator(mode='before')
    @classmethod
    def exact_version(cls, value: Any) -> Any:
        if isinstance(value, dict) and 'schema_version' in value and type(value['schema_version']) is not int:
            raise ValueError('runtime schema version must be an exact integer')
        return value

    def digest(self) -> str:
        payload = json.dumps(self.model_dump(mode='json'), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()


class RuntimeIdentity(RuntimeContract):
    schema_version: Literal[1]
    carrier_image: str = Field(pattern=IMAGE)
    carrier_platform: str = Field(pattern=LINUX_PLATFORM)
    guest_base_sha256: str = Field(pattern=SHA256)
    guest_platform: str = Field(pattern=LINUX_PLATFORM)
    emulator_sha256: str = Field(pattern=SHA256)
    firmware_sha256: str = Field(pattern=SHA256)
    configuration_sha256: str = Field(pattern=SHA256)


class RuntimeResources(RuntimeContract):
    cpus: int = Field(gt=0, le=64)
    memory_mib: int = Field(ge=512, le=65536)
    cache_bytes: int = Field(gt=0)
    scratch_bytes: int = Field(gt=0)
    reserve_bytes: int = Field(gt=0)
    max_seconds: int = Field(gt=0, le=3600)


class RuntimeEndpoint(RuntimeContract):
    id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')
    bind_address: Literal['127.0.0.1']
    guest_port: int = Field(gt=0, le=65535)
    outer_port: int = Field(gt=0, le=65535)


class RuntimePolicy(RuntimeContract):
    """Resolved before discovery, independently of future project endpoint requests."""

    schema_version: Literal[1]
    kind: Literal['private_service_guest']
    identity: RuntimeIdentity
    resources: RuntimeResources
    control_port: int = Field(gt=0, le=65535)
    max_endpoints: int = Field(ge=0, le=64)


class RuntimeSpec(RuntimeContract):
    """An accepted endpoint request compiled against the operator's exact policy."""

    schema_version: Literal[1]
    policy: RuntimePolicy
    endpoints: list[RuntimeEndpoint] = Field(max_length=64)

    @model_validator(mode='after')
    def endpoint_isolation(self) -> Self:
        if len(self.endpoints) > self.policy.max_endpoints:
            raise ValueError('runtime endpoint budget exceeded')
        if len({endpoint.id for endpoint in self.endpoints}) != len(self.endpoints):
            raise ValueError('duplicate runtime endpoint ID')
        ports = [endpoint.outer_port for endpoint in self.endpoints]
        if len(set(ports)) != len(ports) or self.policy.control_port in ports:
            raise ValueError('runtime endpoint collides with an owned listener')
        if any(endpoint.guest_port == 22 for endpoint in self.endpoints):
            raise ValueError('runtime endpoint cannot expose guest control service')
        return self


class RuntimeArtifact(RuntimeContract):
    # Single owned cache member, never an arbitrary path or a host image reference.
    path: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
    sha256: str = Field(pattern=SHA256)
    bytes: int = Field(gt=0)


class RuntimeArtifactReceipt(RuntimeContract):
    """Installer output identity, bound after shutdown without changing the manifest."""

    schema_version: Literal[1]
    manifest_digest: str = Field(pattern=SHA256)
    source_digest: str = Field(pattern=SHA256)
    runtime_digest: str = Field(pattern=SHA256)
    sealed: Literal[True]
    artifacts: list[RuntimeArtifact] = Field(min_length=1, max_length=16)

    @model_validator(mode='after')
    def unique_members(self) -> Self:
        if len({artifact.path for artifact in self.artifacts}) != len(self.artifacts):
            raise ValueError('duplicate runtime artifact member')
        return self

    def require_binding(self, *, manifest_digest: str, source_digest: str, runtime_digest: str) -> None:
        if (self.manifest_digest, self.source_digest, self.runtime_digest) != (manifest_digest, source_digest, runtime_digest):
            raise ValueError('runtime artifact identity differs from accepted contract')
