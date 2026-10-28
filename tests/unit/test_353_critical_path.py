"""Divided construction's critical path: check groups start while the global author runs, from the base global part."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from coordinare_service_inference.environment.models import ShardChecks
from pydantic import ValidationError

from coordinare.services import environment_discovery as discovery_module
from coordinare.services.environment_agent_pool import run_pool
from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_scope import _ResponseReferenceError
from coordinare.services.environment_shard_construction import (
    CheckGroup,
    merge_parts,
    validate_global,
    validate_group,
)
from tests.unit.test_353_sharded_executable import global_part, group_reply, make_criterion

WAIT = 5  # seconds; a serial path fails with a timeout instead of hanging


def three_groups() -> list[CheckGroup]:
    return [CheckGroup(f'c{index:02d}-', (make_criterion(index + 1),)) for index in range(3)]


def reply_for(group: CheckGroup, summary: dict[str, Any]) -> dict[str, Any]:
    """Only c00 maps to the global check 'gone' when its brief shows it; every other author uses its own checks."""
    if group.prefix == 'c00-' and any(check.get('id') == 'gone' for check in summary['checks']):
        return {'shard_checks': {'checks': [], 'criterion_checks': {c.id: ['gone'] for c in group.criteria}}}
    return group_reply(group)


class TimedBuild:
    """Records every group start and end, the peak number of model calls in flight, and each round's briefs."""

    def __init__(self, groups: list[CheckGroup], *, hold: Callable[[CheckGroup], Awaitable[None]] | None = None) -> None:
        self.groups = groups
        self.hold = hold
        self.feedback: dict[str, Any] = {}
        self.previous: dict[str, Any] = {}
        self.group_inputs: dict[str, str] = {}
        self.global_part: dict[str, Any] | None = None
        self.concurrency = 12
        self.events: list[tuple[str, str]] = []
        self.in_flight = 0
        self.peak = 0
        self.rounds: list[list[str]] = []
        self.summaries: list[dict[str, Any]] = []

    def enter(self) -> None:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)

    def leave(self) -> None:
        self.in_flight -= 1

    def admits(self, group: CheckGroup, part: ShardChecks, *, summary: dict[str, Any]) -> bool:
        try:
            validate_group({'shard_checks': part.model_dump(mode='json')}, group=group, summary=summary)
        except _ResponseReferenceError:
            return False
        return True

    async def author_all(self, *, summary, probes, groups=None, concurrency=None):
        asked = list(self.groups if groups is None else groups)
        self.rounds.append([group.prefix for group in asked])
        self.summaries.append(json_safe(summary))

        async def one(group: CheckGroup) -> tuple[CheckGroup, ShardChecks]:
            self.enter()
            self.events.append(('start', group.prefix))
            try:
                if self.hold is not None:
                    await self.hold(group)
                return group, validate_group(reply_for(group, summary), group=group, summary=summary)
            finally:
                self.events.append(('end', group.prefix))
                self.leave()

        budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8, max_schema_repairs=8)
        authored = await run_pool(asked, one, budget=budget, split=lambda item, _overflow: [item],
                                  concurrency=concurrency or self.concurrency)
        return {group.prefix: (group, part) for group, part in authored}


def json_safe(summary: dict[str, Any]) -> dict[str, Any]:
    return {'checks': [dict(check) for check in summary['checks']], 'readiness_ids': list(summary['readiness_ids'])}


def attempt_for(build: TimedBuild, budget: AttemptBudget) -> SimpleNamespace:
    async def observe(_inputs: Any) -> None:
        return None

    inspection = SimpleNamespace(budget=budget, inputs={}, finding_responses=[])
    return SimpleNamespace(budget=budget, required_ids={}, discovery=inspection, messages=[{'role': 'system', 'content': ''},
                           {'role': 'user', 'content': '{}'}], chat=None, delta=None, prior_manifests=[],
                           construction=SimpleNamespace(observe_sources=observe))


def stub_global(monkeypatch, build: TimedBuild, produce: Callable[[], Awaitable[dict[str, Any]]]) -> None:
    """The global author's model call: entered and left through the same in-flight count as the check groups."""
    async def phase(**_kwargs: Any) -> tuple[dict[str, Any], dict[str, str]]:
        build.enter()
        build.events.append(('start', 'global'))
        try:
            await asyncio.sleep(0)  # a model call awaits I/O; the check groups start before it returns
            return await produce(), {}
        finally:
            build.events.append(('end', 'global'))
            build.leave()

    monkeypatch.setattr(discovery_module, '_phase', phase)


def identity(data: dict[str, Any]) -> dict[str, Any]:
    return data


@pytest.mark.asyncio
async def test_check_groups_start_while_the_global_author_is_still_running(monkeypatch):
    groups = three_groups()[:2]
    both_started = asyncio.Event()
    started: list[str] = []

    async def hold(group: CheckGroup) -> None:
        started.append(group.prefix)
        if len(started) == len(groups):
            both_started.set()
        await asyncio.wait_for(both_started.wait(), WAIT)

    build = TimedBuild(groups, hold=hold)
    build.global_part = global_part()  # the failed or previous part the groups start from

    async def produce() -> dict[str, Any]:
        # The global author finishes only after both groups have started: a serial path times out here.
        await asyncio.wait_for(both_started.wait(), WAIT)
        return global_part()

    stub_global(monkeypatch, build, produce)
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8, max_schema_repairs=8)
    merged = await discovery_module._divided_candidate(build, attempt_for(build, budget), identity)

    assert build.rounds == [['c00-', 'c01-']]  # each group authored once, nothing re-authored
    global_end = build.events.index(('end', 'global'))
    assert {event for event in build.events[:global_end] if event[0] == 'start'} == {('start', 'global'), ('start', 'c00-'), ('start', 'c01-')}
    assert {check['id'] for check in merged['checks']} == {'toolchain', 'c00-0', 'c01-0'}


@pytest.mark.asyncio
async def test_a_group_that_used_a_global_check_the_new_global_part_drops_is_authored_again(monkeypatch):
    groups = three_groups()[:2]
    build = TimedBuild(groups)
    build.global_part = global_part(checks=[{'id': 'toolchain', 'argv': ['true']}, {'id': 'gone', 'argv': ['gone']}])

    async def produce() -> dict[str, Any]:
        return global_part()  # no 'gone'

    stub_global(monkeypatch, build, produce)
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8, max_schema_repairs=8)
    merged = await discovery_module._divided_candidate(build, attempt_for(build, budget), identity)

    assert build.rounds == [['c00-', 'c01-'], ['c00-']]  # only the group that used 'gone' is authored again
    assert any(check['id'] == 'gone' for check in build.summaries[0]['checks'])  # started from the base part
    assert not any(check['id'] == 'gone' for check in build.summaries[-1]['checks'])
    assert 'gone' not in {identifier for ids in merged['criterion_checks'].values() for identifier in ids}
    assert {check['id'] for check in merged['checks']} == {'toolchain', 'c00-0', 'c01-0'}


@pytest.mark.asyncio
async def test_a_global_change_no_group_references_keeps_every_group_part(monkeypatch):
    """v40's repair changed the activation, install ids and checks of the global part; no group's references did."""
    groups = three_groups()[:2]
    build = TimedBuild(groups)
    build.global_part = global_part(activation={'env': {'A': '1'}, 'path': []},
                                    install=[{'id': 'install-old', 'argv': ['old']}])

    async def produce() -> dict[str, Any]:
        return global_part(activation={'env': {'A': '2'}, 'path': ['/x']},
                           install=[{'id': 'install-new', 'argv': ['new']}],
                           checks=[{'id': 'toolchain', 'argv': ['true']}, {'id': 'added', 'argv': ['added']}])

    stub_global(monkeypatch, build, produce)
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8, max_schema_repairs=8)
    merged = await discovery_module._divided_candidate(build, attempt_for(build, budget), identity)

    assert build.rounds == [['c00-', 'c01-']]  # no model call beyond the speculative round
    assert merged['activation'] == {'env': {'A': '2'}, 'path': ['/x']}
    assert {check['id'] for check in merged['checks']} == {'toolchain', 'added', 'c00-0', 'c01-0'}


def test_merge_time_admission_rejects_a_dependency_on_a_global_check_the_merged_global_part_lacks():
    criterion = make_criterion(1)
    part = ShardChecks.model_validate({'checks': [{'id': 'c00-0', 'argv': ['x'], 'depends_on': ['gone']}],
                                       'criterion_checks': {criterion.id: ['c00-0']}})
    with pytest.raises(ValidationError):
        validate_global(merge_parts(global_part(), [part]))


@pytest.mark.asyncio
async def test_the_global_author_and_its_check_groups_share_the_configured_cap(monkeypatch):
    groups = three_groups()
    build = TimedBuild(groups)
    build.concurrency = 2
    first_group = asyncio.Event()

    async def hold(_group: CheckGroup) -> None:
        first_group.set()

    build.hold = hold
    build.global_part = global_part()

    async def produce() -> dict[str, Any]:
        await asyncio.wait_for(first_group.wait(), WAIT)
        return global_part()

    stub_global(monkeypatch, build, produce)
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8, max_schema_repairs=8)
    merged = await discovery_module._divided_candidate(build, attempt_for(build, budget), identity)

    assert build.peak == 2  # the global author and one check group overlapped, never more than the cap
    assert build.rounds == [['c00-', 'c01-', 'c02-']]
    assert len(merged['checks']) == 4


@pytest.mark.asyncio
async def test_without_a_base_part_the_first_generation_keeps_the_global_author_first(monkeypatch):
    groups = three_groups()[:2]
    build = TimedBuild(groups)  # nothing admitted yet: groups need the global part's references

    async def produce() -> dict[str, Any]:
        return global_part()

    stub_global(monkeypatch, build, produce)
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8, max_schema_repairs=8)
    await discovery_module._divided_candidate(build, attempt_for(build, budget), identity)

    assert build.events[0] == ('start', 'global')
    assert build.events[1] == ('end', 'global')
    assert build.rounds == [['c00-', 'c01-']]


@pytest.mark.asyncio
async def test_a_failing_global_author_cancels_the_check_groups_it_started_with(monkeypatch):
    groups = three_groups()[:2]
    build = TimedBuild(groups)
    build.global_part = global_part()
    release = asyncio.Event()

    async def hold(_group: CheckGroup) -> None:
        await asyncio.wait_for(release.wait(), WAIT)  # never released: the groups are still running when the global fails

    build.hold = hold

    async def produce() -> dict[str, Any]:
        await asyncio.sleep(0)
        message = 'the global author failed'
        raise EnvironmentDiscoveryError(message, code='source_identity')

    stub_global(monkeypatch, build, produce)
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8, max_schema_repairs=8)
    with pytest.raises(EnvironmentDiscoveryError):
        await discovery_module._divided_candidate(build, attempt_for(build, budget), identity)
    await asyncio.sleep(0)
    assert build.in_flight == 0
    assert build.rounds == [['c00-', 'c01-']]


def test_a_full_repair_starts_its_groups_from_the_prior_manifests_global_declaration(monkeypatch):
    groups = three_groups()[:2]
    prior = {'checks': [{'id': 'toolchain', 'argv': ['true']}, {'id': 'c00-0', 'argv': ['x']}], 'criterion_checks': {'S1': ['c00-0']}}
    monkeypatch.setattr(discovery_module, 'proposal_of', lambda _manifest: prior)
    build = TimedBuild(groups)
    attempt = SimpleNamespace(delta=None, prior_manifests=[object()])

    base = discovery_module._speculative_base(build, attempt)

    assert base == {'checks': [{'id': 'toolchain', 'argv': ['true']}], 'criterion_checks': {}}
    assert discovery_module._speculative_base(build, SimpleNamespace(delta=None, prior_manifests=[])) is None
