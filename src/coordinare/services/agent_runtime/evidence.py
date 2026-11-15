"""Immutable source pages bounded by their actual serialized tool response."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, NoReturn

from nare import Tool
from nare.tools import MAX_TOOL_OUTPUT

from coordinare.services.agent_runtime.checkpoint import EvidenceReceipt
from coordinare.services.environment_attempt import EnvironmentDiscoveryError


@dataclass(frozen=True)
class EvidenceSource:
    key: str
    content: str
    sha256: str


@dataclass(frozen=True)
class EvidenceRead:
    pages: tuple[int, ...] = ()


@dataclass(frozen=True)
class EvidencePage:
    id: int
    source: str
    source_sha256: str
    offset: int
    end: int
    sha256: str
    content: str


@dataclass(frozen=True)
class EvidencePageBatch:
    pages: tuple[EvidencePage, ...]
    next_cursor: int | None
    remaining_required: int
    serialized: str


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def _envelope(pages: tuple[EvidencePage, ...], remaining: set[int], page_count: int) -> str:
    return _encoded_envelope(tuple(_encode_page(page) for page in pages), remaining, page_count)


def _encode_page(page: EvidencePage) -> str:
    return json.dumps(asdict(page), ensure_ascii=False, separators=(',', ':'))


def _encoded_envelope(pages: tuple[str, ...], remaining: set[int], page_count: int) -> str:
    cursor = str(min(remaining)) if remaining else 'null'
    return ('{"pages":[' + ','.join(pages) + '],"unread_count":' + str(len(remaining))
            + ',"next_unread":' + cursor + ',"page_count":' + str(page_count) + '}')


MIN_OUTPUT_CAP = 512


class EvidenceStore:
    """Phase-local coverage over content verified against pinned source hashes.

    Sources come from Coordinare's pinned reader. This store grants no filesystem
    or network access, and event redaction is never used to reconstruct evidence.
    Offsets count Unicode code points; hashes bind the original UTF-8 bytes.
    """

    def __init__(self, sources: tuple[EvidenceSource, ...], *, required: frozenset[str],
                 output_cap: int = MAX_TOOL_OUTPUT) -> None:
        if type(output_cap) is not int or not MIN_OUTPUT_CAP <= output_cap <= MAX_TOOL_OUTPUT:
            message = 'Evidence response cap is outside the runtime limit.'
            raise EnvironmentDiscoveryError(message, code='agent_evidence_cap')
        keys = [source.key for source in sources]
        if len(set(keys)) != len(keys) or not required.issubset(keys):
            message = 'Evidence catalog has duplicate or missing required identities.'
            raise EnvironmentDiscoveryError(message, code='agent_evidence_catalog')
        self._cap = output_cap
        pages: list[EvidencePage] = []
        required_ids: set[int] = set()
        for source in sources:
            if _digest(source.content) != source.sha256:
                message = 'Evidence differs from its pinned source hash.'
                raise EnvironmentDiscoveryError(message, code='agent_evidence_changed')
            for page in self._paginate(source, len(pages)):
                pages.append(page)
                if source.key in required:
                    required_ids.add(page.id)
        self._pages = tuple(pages)
        self._encoded_pages = tuple(_encode_page(page) for page in pages)
        self._required = frozenset(required_ids)
        self._delivered: set[int] = set()
        binding = [(page.id, page.source, page.source_sha256, page.offset, page.end, page.sha256)
                   for page in self._pages]
        self._catalog_digest = _digest(json.dumps({'pages': binding, 'required': sorted(self._required)},
                                                ensure_ascii=False, separators=(',', ':')))

    def _paginate(self, source: EvidenceSource, first: int) -> list[EvidencePage]:
        pages: list[EvidencePage] = []
        offset = 0
        while offset < len(source.content) or not pages:
            size = min(4096, len(source.content) - offset)
            while True:
                content = source.content[offset:offset + size]
                page = EvidencePage(first + len(pages), source.key, source.sha256, offset,
                                    offset + size, _digest(content), content)
                # Leave room for the changing cursor/count even for a large catalog.
                if len(_envelope((page,), set(), first + len(pages) + 1)) + 64 <= self._cap:
                    break
                if size <= 1:
                    message = 'Evidence identity cannot fit a complete runtime response.'
                    raise EnvironmentDiscoveryError(message, code='agent_evidence_too_large')
                size //= 2
            pages.append(page)
            offset += size
            if not source.content:
                break
        return pages

    @property
    def page_count(self) -> int:
        return len(self._pages)

    @property
    def delivered(self) -> frozenset[int]:
        return frozenset(self._delivered)

    @property
    def remaining_required(self) -> int:
        return len(self._required - self._delivered)

    async def read(self, request: EvidenceRead) -> EvidencePageBatch:
        ids = request.pages or tuple(sorted(self._required - self._delivered)[:8])
        if any(type(index) is not int or not 0 <= index < self.page_count for index in ids):
            message = 'Requested evidence page is outside the pinned catalog.'
            raise EnvironmentDiscoveryError(message, code='agent_evidence_page')
        selected: tuple[EvidencePage, ...] = ()
        selected_encoded: tuple[str, ...] = ()
        delivered = set(self._delivered)
        serialized = _envelope(selected, set(self._required - delivered), self.page_count)
        for index in dict.fromkeys(ids):
            candidate = (*selected, self._pages[index])
            after = delivered | {index}
            candidate_encoded = (*selected_encoded, self._encoded_pages[index])
            encoded = _encoded_envelope(candidate_encoded, set(self._required - after), self.page_count)
            if len(encoded) > self._cap:
                break
            selected, delivered, serialized = candidate, after, encoded
            selected_encoded = candidate_encoded
        # Only the returned whole pages count. No await separates this update
        # from returning the exact string the caller-tool transport will deliver.
        self._delivered = delivered
        remaining = self._required - delivered
        return EvidencePageBatch(selected, min(remaining) if remaining else None, len(remaining), serialized)

    def receipt(self) -> EvidenceReceipt:
        return EvidenceReceipt(catalog_digest=self._catalog_digest, delivered=tuple(sorted(self._delivered)))

    def restore_receipt(self, receipt: EvidenceReceipt) -> None:
        """Restore only after the checkpoint store verified the domain-owned reference."""
        if (receipt.catalog_digest != self._catalog_digest
                or any(index >= self.page_count for index in receipt.delivered)):
            message = 'Resumed evidence does not match the current pinned catalog.'
            raise EnvironmentDiscoveryError(message, code='agent_evidence_changed')
        self._delivered.update(receipt.delivered)

    def tool(self) -> Tool:
        async def deliver(arguments: dict[str, Any]) -> str:
            return (await self.read(EvidenceRead())).serialized

        return Tool(name='review_evidence',
                    description='Read the next required immutable evidence batch. Call with {} repeatedly until '
                                'unread_count is zero. next_unread is informational; never calculate a page range. '
                                'Use reread_evidence only to revisit specific pages.',
                    input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False},
                    call=deliver)

    async def reread(self, pages: tuple[int, ...], *, continuation_tool: str) -> str:
        if not pages:
            self._refuse_reread(continuation_tool)
        try:
            return (await self.read(EvidenceRead(pages))).serialized
        except EnvironmentDiscoveryError as exc:
            if exc.code != 'agent_evidence_page':
                raise
            self._refuse_reread(continuation_tool)

    def _refuse_reread(self, continuation_tool: str) -> NoReturn:
        remaining = self._required - self._delivered
        message = json.dumps({'code': 'agent_evidence_page', 'page_count': self.page_count,
            'next_unread': min(remaining) if remaining else None, 'unread_count': len(remaining),
            'recovery': f'Call {continuation_tool} without pages to continue; reuse its source/observation identity.'},
            separators=(',', ':'))
        raise EnvironmentDiscoveryError(message, code='agent_evidence_page') from None

    def reread_tool(self) -> Tool:
        async def deliver(arguments: dict[str, Any]) -> str:
            return await self.reread(tuple(arguments['pages']), continuation_tool='review_evidence')

        return Tool(name='reread_evidence', description='Revisit explicit immutable evidence page IDs. '
                    'IDs range from zero to page_count minus one. For required forward reading use review_evidence({}).',
                    input_schema={'type': 'object', 'properties': {'pages': {'type': 'array',
                        'items': {'type': 'integer', 'minimum': 0}, 'minItems': 1, 'maxItems': 128}},
                        'required': ['pages'], 'additionalProperties': False}, call=deliver)
