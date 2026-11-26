"""nare-native-042 spent three of its eight invalid evidence calls on paths our own tools and corrections handed the
model: list_sources(".") was refused because the root is "", and twice @phase-packet (the task packet's evidence
name, which locate_evidence reports and a citation correction told it to read) was passed to read_source."""
from __future__ import annotations

import pytest

from coordinare.services.environment_agent import PinnedSourceTools
from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_inspection import SourceInspection
from coordinare.services.environment_response_diagnostics import PACKET_SOURCE, packet_citation_note
from coordinare.services.environment_scope import uninspected_sources_errors

PATHS = ['README.md', 'src/app.py', '@types/index.d.ts']


async def _fetch(path: str) -> str | None:
    return {'README.md': 'read me', 'src/app.py': 'print(1)', '@types/index.d.ts': 'export {}'}.get(path)


def test_the_repository_root_and_slashed_directories_can_be_listed() -> None:
    inspection = SourceInspection(PATHS, _fetch, DiscoveryLimits())
    root = inspection.list_directory('')['entries']
    for alias in ('.', '/', './'):
        assert inspection.list_directory(alias)['entries'] == root
    assert inspection.list_directory('./src/')['entries'] == inspection.list_directory('src')['entries']


@pytest.mark.asyncio
async def test_read_source_on_the_packet_points_to_reread_evidence_but_reads_real_at_paths() -> None:
    tools = PinnedSourceTools(SourceInspection(PATHS, _fetch, DiscoveryLimits()))
    with pytest.raises(EnvironmentDiscoveryError) as refused:
        await tools.read({'path': PACKET_SOURCE})
    assert refused.value.code == 'invalid_tool_arguments' and 'reread_evidence' in str(refused.value)
    # A repository path that merely begins with @ is still an ordinary file.
    assert 'export {}' in await tools.read({'path': '@types/index.d.ts'})


def test_a_packet_citation_is_told_to_be_dropped_not_read() -> None:
    assert packet_citation_note(['README.md', PACKET_SOURCE], [1])
    assert packet_citation_note(['README.md', 'docs/x.md'], [1]) == ''
    [error] = uninspected_sources_errors(['README.md', PACKET_SOURCE], {'README.md': 'd' * 64})
    assert 'never read it with read_source' in error['guidance']


def test_the_required_reads_tool_says_it_is_the_only_tool_until_reads_finish() -> None:
    from coordinare.services.agent_runtime.required_reads import RequiredReads

    description = RequiredReads(lambda: None, lambda: None).tool.description
    assert 'this is your only tool' in description and 'never put their arguments or your answer here' in description
