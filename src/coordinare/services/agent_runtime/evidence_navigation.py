"""Bounded lookup over immutable evidence; locating never counts as reading."""
from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from itertools import islice
from typing import TYPE_CHECKING, Any

from nare import Tool

from coordinare.services.agent_runtime.errors import refuse_agent

if TYPE_CHECKING:
    from collections.abc import Iterator

    from coordinare.services.agent_runtime.evidence import EvidencePage, EvidenceSource

MAX_MATCHES = 32
MAX_QUERY = 256
INVENTORY_CAP = 2048
NAVIGATION_POLICY = 'immutable-evidence-locator-v1'


def packet_inventory(packet: dict[str, Any]) -> str:
    """A small durable index, not a copy or summary of untrusted evidence."""
    fields: list[str] = []
    for key in sorted(packet):
        if len(json.dumps([*fields, key], ensure_ascii=False)) > INVENTORY_CAP - 128:
            break
        fields.append(key)
    return json.dumps({'packet_source': '@phase-packet', 'packet_fields': fields,
                      'omitted_fields': len(packet) - len(fields)}, ensure_ascii=False)


class EvidenceLocator:
    def __init__(self, sources: tuple[EvidenceSource, ...], pages: tuple[EvidencePage, ...], cap: int) -> None:
        self.sources, self.cap = sources, cap
        self.pages: dict[str, list[EvidencePage]] = {}
        for page in pages:
            self.pages.setdefault(page.source, []).append(page)

    def entries(self, source_key: str | None, query: str | None) -> Iterator[dict[str, Any]]:
        for source in self.sources:
            if source_key is not None and source.key != source_key:
                continue
            pages = self.pages[source.key]
            common = {'source': source.key, 'source_sha256': source.sha256}
            if query is None:
                yield {**common, 'first_page': pages[0].id, 'last_page': pages[-1].id,
                       'characters': len(source.content)}
                continue
            offsets = [page.offset for page in pages]
            offset = source.content.find(query)
            while offset >= 0:
                end = offset + len(query)
                selected = pages[bisect_right(offsets, offset) - 1:bisect_left(offsets, end)]
                yield {**common, 'offset': offset, 'end': end, 'pages': [page.id for page in selected]}
                offset = source.content.find(query, offset + 1)

    async def locate(self, arguments: dict[str, Any]) -> str:
        source, query, cursor = arguments.get('source'), arguments.get('query'), arguments.get('cursor', 0)
        if (set(arguments) - {'source', 'query', 'cursor'} or type(cursor) is not int or cursor < 0
                or (source is not None and not isinstance(source, str))
                or (query is not None and (not isinstance(query, str) or not 0 < len(query) <= MAX_QUERY))):
            refuse_agent('Use an optional source, nonempty literal query and nonnegative cursor.',
                         code='invalid_tool_arguments')
        matches: list[dict[str, Any]] = []
        next_cursor: int | None = None
        # Every source contributes at most one catalog entry or one match per
        # character. Avoid islice's machine-integer limit for empty far cursors.
        if cursor >= sum(max(1, len(item.content)) for item in self.sources):
            return json.dumps({'matches': matches, 'next_cursor': next_cursor})
        for entry in islice(self.entries(source, query), cursor, None):
            after = cursor + len(matches) + 1
            candidate = json.dumps({'matches': [*matches, entry], 'next_cursor': after}, ensure_ascii=False)
            if len(matches) >= MAX_MATCHES or len(candidate) + 16 > self.cap:
                if not matches:
                    refuse_agent('Evidence locator entry exceeds the output cap.', code='agent_evidence_cap')
                next_cursor = cursor + len(matches)
                break
            matches.append(entry)
        return json.dumps({'matches': matches, 'next_cursor': next_cursor}, ensure_ascii=False)

    def tool(self) -> Tool:
        return Tool(name='locate_evidence', description='Locate canonical evidence after context elision. '
            'With {} list source identities and page ranges. Optional source filters an exact identity; '
            'optional query finds literal text including across page boundaries. Use next_cursor for more matches. '
            'Then call reread_evidence with the returned page IDs. This metadata does not count as reading evidence.',
            input_schema={'type': 'object', 'properties': {
                'source': {'type': 'string'}, 'query': {'type': 'string', 'minLength': 1, 'maxLength': MAX_QUERY},
                'cursor': {'type': 'integer', 'minimum': 0}}, 'additionalProperties': False}, call=self.locate)
