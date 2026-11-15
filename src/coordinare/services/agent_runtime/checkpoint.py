"""Content-addressed agent checkpoints referenced by trusted domain state."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

from nare import loads
from nare.contract import NARE_VERSION
from pydantic import BaseModel, ConfigDict, Field, model_validator

from coordinare.services.agent_runtime.errors import invalid_checkpoint
from coordinare.services.agent_runtime.models import (
    AgentIdentity,  # noqa: TC001 - pydantic resolves the field at runtime
)
from coordinare.services.environment_attempt import EnvironmentDiscoveryError

Digest = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
MAX_CHECKPOINT_BYTES = 32 * 1024 * 1024
KEY = re.compile(r'v1-[0-9a-f]{64}\.json\Z')
COUNTERS = frozenset({'calls', 'reads', 'verification_reads', 'schema_repairs', 'scope_revisions',
                      'coverage_revisions', 'execution_repairs', 'probe_batches', 'trial_runs', 'shard_schema_repairs',
                      'agent_evidence_errors'})


class CheckpointReference(BaseModel):
    """Only this bounded reference goes into the daemon's ordinary snapshot."""

    model_config = ConfigDict(extra='forbid', frozen=True)
    version: Literal[1] = 1
    key: str = Field(pattern=r'^v1-[0-9a-f]{64}\.json$')
    identity: AgentIdentity


class EvidenceReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    catalog_digest: Digest
    delivered: tuple[Annotated[int, Field(strict=True, ge=0)], ...] = ()

    @model_validator(mode='after')
    def unique_pages(self) -> EvidenceReceipt:
        if len(set(self.delivered)) != len(self.delivered):
            invalid_checkpoint('Evidence receipt repeats a page.')
        return self


class EffectReceipt(BaseModel):
    """A reservation is not completion, even if transcript prose says otherwise."""

    model_config = ConfigDict(extra='forbid', frozen=True)
    operation_id: str = Field(min_length=1, max_length=128)
    tool: str = Field(min_length=1, max_length=64)
    binding_digest: Digest
    status: Literal['pending', 'completed']
    result_digest: Digest | None = None
    result_json: str | None = Field(default=None, repr=False)

    @model_validator(mode='after')
    def completion_has_receipt(self) -> EffectReceipt:
        if (self.status == 'completed') != (self.result_digest is not None):
            invalid_checkpoint('Completed effects require an execution receipt.')
        if self.result_json is not None and hashlib.sha256(self.result_json.encode()).hexdigest() != self.result_digest:
            invalid_checkpoint('Effect result differs from its receipt.')
        return self


class AgentCheckpoint(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    version: Literal[1] = 1
    nare_version: str = NARE_VERSION
    identity: AgentIdentity
    session_text: str = Field(repr=False)
    evidence: EvidenceReceipt
    counters: dict[str, Annotated[int, Field(strict=True, ge=0)]]
    phase_calls: int = Field(default=0, ge=0, strict=True)
    effects: tuple[EffectReceipt, ...] = ()
    tool_state: dict[str, Any] = Field(default_factory=dict, repr=False)
    prerequisite_reminders: frozenset[str] = frozenset()
    phase_expires_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    terminal_status: Literal['accepted', 'held', 'cancelled', 'error'] | None = None
    hold_code: str | None = None
    evidence_error_receipts: frozenset[Digest] = frozenset()

    @model_validator(mode='after')
    def bounded_authority(self) -> AgentCheckpoint:
        if self.nare_version != NARE_VERSION:
            invalid_checkpoint('Checkpoint belongs to a different native runtime version.')
        if not set(self.counters) <= COUNTERS:
            invalid_checkpoint('Checkpoint contains an unknown domain counter.')
        if len({item.operation_id for item in self.effects}) != len(self.effects):
            invalid_checkpoint('Checkpoint repeats an external operation.')
        return self


class CheckpointStore:
    """Immutable private files; the domain journal supplies the expected key/identity.

    Loading never runs tools. Pending external effects must be reconciled by the
    existing probe/trial owner before the domain chooses a new phase or resume.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    async def save(self, checkpoint: AgentCheckpoint) -> str:
        try:
            validated = AgentCheckpoint.model_validate(checkpoint.model_dump(mode='json'))
            loads(validated.session_text)
            content = validated.model_dump_json().encode('utf-8')
            if len(content) > MAX_CHECKPOINT_BYTES:
                invalid_checkpoint()
            return await asyncio.to_thread(self._write, content)
        except (OSError, ValueError, TypeError):
            message = 'Agent checkpoint could not be persisted; no further external work is allowed.'
            raise EnvironmentDiscoveryError(message, code='agent_checkpoint_persistence') from None

    def _write(self, content: bytes) -> str:
        if self.root.is_symlink():
            invalid_checkpoint()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = 'v1-' + hashlib.sha256(content).hexdigest() + '.json'
        fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=self.root)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.root / key)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return key

    async def load_verified(self, key: str, expected: AgentIdentity) -> AgentCheckpoint:
        try:
            if not KEY.fullmatch(key) or self.root.is_symlink():
                invalid_checkpoint()
            content = await asyncio.to_thread(self._read, key)
            if hashlib.sha256(content).hexdigest() != key[3:-5]:
                invalid_checkpoint()
            checkpoint = AgentCheckpoint.model_validate_json(content)
            loads(checkpoint.session_text)
        except (OSError, ValueError, TypeError):
            message = 'Agent checkpoint integrity could not be verified.'
            raise EnvironmentDiscoveryError(message, code='agent_checkpoint_integrity') from None
        if checkpoint.identity != expected:
            message = 'Agent checkpoint belongs to a different source, phase, policy or model.'
            raise EnvironmentDiscoveryError(message, code='agent_checkpoint_identity')
        if any(effect.status == 'pending' for effect in checkpoint.effects):
            message = 'Agent checkpoint has an uncertain external effect; reconcile its existing owner first.'
            raise EnvironmentDiscoveryError(message, code='agent_effect_pending')
        return checkpoint

    def _read(self, key: str) -> bytes:
        fd = os.open(self.root / key, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CHECKPOINT_BYTES:
                invalid_checkpoint()
            content = stream.read(MAX_CHECKPOINT_BYTES + 1)
            if len(content) > MAX_CHECKPOINT_BYTES:
                invalid_checkpoint()
            return content
