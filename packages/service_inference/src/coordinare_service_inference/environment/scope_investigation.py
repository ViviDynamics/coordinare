"""Generic pinned source inspection requests and daemon-owned result identities."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from .evidence_safety import source_path
from .findings import Commit, Digest, FindingEvidence, FindingId


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


class ScopeFileRequest(FindingEvidence):
    operation: Literal['read_file']
    path: str

    @field_validator('path')
    @classmethod
    def safe_path(cls, value: str) -> str:
        return source_path(value)


class ScopeListingRequest(FindingEvidence):
    operation: Literal['list_files']
    path: str
    cursor: int = Field(ge=0)

    @field_validator('path')
    @classmethod
    def safe_directory(cls, value: str) -> str:
        return source_path(value) if value else ''


ScopeInspectionRequest = Annotated[ScopeFileRequest | ScopeListingRequest, Field(discriminator='operation')]


def unique_requests(requests: list[ScopeInspectionRequest]) -> list[ScopeInspectionRequest]:
    if len({_canonical(request.model_dump(mode='json')) for request in requests}) != len(requests):
        raise ValueError('inspection requests must be exact-operation unique')
    return requests


class ScopeInspectionBinding(FindingEvidence):
    id: str = Field(pattern=r'^I[0-9a-f]{64}$')
    finding_id: FindingId
    review_sequence: int = Field(ge=1)
    scope_digest: Digest
    source_commit: Commit
    inventory_digest: Digest
    request: ScopeInspectionRequest

    @classmethod
    def bind(cls, **identity: Any) -> Self:
        identifier = 'I'+hashlib.sha256(_canonical(identity)).hexdigest()
        return cls.model_validate({'id': identifier, **identity})

    @model_validator(mode='after')
    def exact_identity(self) -> Self:
        identity = self.model_dump(mode='json', exclude={'id'})
        if self.id != 'I'+hashlib.sha256(_canonical(identity)).hexdigest():
            raise ValueError('inspection binding differs from complete identity')
        return self


class ScopeInspectionResult(FindingEvidence):
    binding_id: str = Field(pattern=r'^I[0-9a-f]{64}$')
    status: Literal['file', 'listing', 'absent']
    evidence_digest: Digest
    file_sha256: Digest | None = None
    # Same-named pinned inventory paths for an absent request; None means recorded before candidates existed.
    candidates: list[str] | None = Field(default=None, max_length=8)
    more_candidates: int = Field(default=0, ge=0)

    @field_validator('candidates')
    @classmethod
    def safe_candidates(cls, value: list[str] | None) -> list[str] | None:
        return [source_path(path) for path in value] if value is not None else None

    @model_validator(mode='after')
    def file_identity(self) -> Self:
        if (self.status == 'file') != (self.file_sha256 is not None):
            raise ValueError('only a file result requires a file content hash')
        if self.status != 'absent' and (self.candidates is not None or self.more_candidates):
            raise ValueError('only an absent result can name inventory candidates')
        return self


class ScopeInspectionQueue(FindingEvidence):
    bindings: list[ScopeInspectionBinding] = Field(max_length=256)

    @model_validator(mode='after')
    def bounded_history(self) -> Self:
        if len({binding.id for binding in self.bindings}) != len(self.bindings):
            raise ValueError('inspection binding identities must be unique')
        if len(_canonical(self.model_dump(mode='json'))) > 256*1024:
            raise ValueError('inspection queue exceeds metadata bound')
        return self
