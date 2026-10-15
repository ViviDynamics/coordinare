"""237: the performer's CPU sampler measures the process tree between polls, never a first-call 0.0."""
from __future__ import annotations

from collections.abc import Callable
from unittest.mock import patch

import psutil

from performer.process_metrics import CpuSampler

# A fake process table. Each process returns its scripted readings in order. A reading is what
# psutil returns for that call. A process's first call is the priming call, and psutil returns
# 0.0 for it, so the script carries that 0.0 first.


class _FakeProcess:
    def __init__(self, world: dict[int, _FakeProcess], pid: int, readings: list[float],
                 children: list[int], create_time: float = 1.0) -> None:
        self._world = world
        self.pid = pid
        self._readings = list(readings)
        self._children = children
        self._create_time = create_time

    def cpu_percent(self, interval: float | None = None) -> float:
        assert interval is None
        return self._readings.pop(0)

    def children(self, recursive: bool = True) -> list[_FakeProcess]:
        assert recursive is True
        return [self._world[pid] for pid in self._children if pid in self._world]

    def create_time(self) -> float:
        return self._create_time


def _table(world: dict[int, _FakeProcess]) -> Callable[[int], _FakeProcess]:
    def _process(pid: int) -> _FakeProcess:
        if pid not in world:
            raise psutil.NoSuchProcess(pid)
        return world[pid]
    return _process


def test_the_first_sample_has_no_interval_to_report() -> None:
    world: dict[int, _FakeProcess] = {}
    world[1] = _FakeProcess(world, 1, readings=[0.0], children=[2])
    world[2] = _FakeProcess(world, 2, readings=[0.0], children=[])
    with patch("performer.process_metrics.psutil.Process", side_effect=_table(world)):
        sampler = CpuSampler(1)
        assert sampler.sample() is None


def test_the_second_sample_sums_the_performer_and_its_children() -> None:
    world: dict[int, _FakeProcess] = {}
    world[1] = _FakeProcess(world, 1, readings=[0.0, 2.5], children=[2])
    world[2] = _FakeProcess(world, 2, readings=[0.0, 40.0], children=[])
    with patch("performer.process_metrics.psutil.Process", side_effect=_table(world)):
        sampler = CpuSampler(1)
        assert sampler.sample() is None
        assert sampler.sample() == 42.5


def test_an_idle_performer_with_a_busy_agent_child_reports_the_agent_load() -> None:
    """The regression: the agent's CPU is not the performer's, and only the tree shows it."""
    world: dict[int, _FakeProcess] = {}
    world[1] = _FakeProcess(world, 1, readings=[0.0, 0.0], children=[2])
    world[2] = _FakeProcess(world, 2, readings=[0.0, 75.0], children=[])
    with patch("performer.process_metrics.psutil.Process", side_effect=_table(world)):
        sampler = CpuSampler(1)
        sampler.sample()
        assert sampler.sample() == 75.0


def test_a_child_that_appears_between_polls_is_primed_not_counted_as_zero() -> None:
    world: dict[int, _FakeProcess] = {}
    world[1] = _FakeProcess(world, 1, readings=[0.0, 1.0, 3.0, 3.0], children=[])
    with patch("performer.process_metrics.psutil.Process", side_effect=_table(world)):
        sampler = CpuSampler(1)
        sampler.sample()
        assert sampler.sample() == 1.0
        # The child appears now. Its priming call is 0.0 and is not a reading.
        world[2] = _FakeProcess(world, 2, readings=[0.0, 9.0], children=[])
        world[1]._children = [2]
        assert sampler.sample() == 3.0
        assert sampler.sample() == 12.0


def test_a_child_that_exits_is_dropped_without_error() -> None:
    world: dict[int, _FakeProcess] = {}
    world[1] = _FakeProcess(world, 1, readings=[0.0, 4.0, 5.0], children=[2])
    world[2] = _FakeProcess(world, 2, readings=[0.0, 6.0], children=[])
    with patch("performer.process_metrics.psutil.Process", side_effect=_table(world)):
        sampler = CpuSampler(1)
        sampler.sample()
        assert sampler.sample() == 10.0
        del world[2]
        world[1]._children = []
        assert sampler.sample() == 5.0


def test_a_reused_pid_is_primed_again_rather_than_measured_from_the_old_process() -> None:
    world: dict[int, _FakeProcess] = {}
    world[1] = _FakeProcess(world, 1, readings=[0.0, 1.0, 2.0], children=[2])
    world[2] = _FakeProcess(world, 2, readings=[0.0, 50.0], children=[], create_time=10.0)
    with patch("performer.process_metrics.psutil.Process", side_effect=_table(world)):
        sampler = CpuSampler(1)
        sampler.sample()
        assert sampler.sample() == 51.0
        # The OS reuses pid 2 for a new process. Its first reading is a priming call, so it adds nothing.
        world[2] = _FakeProcess(world, 2, readings=[0.0], children=[], create_time=99.0)
        assert sampler.sample() == 2.0


def test_a_missing_root_reports_nothing_and_does_not_raise() -> None:
    with patch("performer.process_metrics.psutil.Process", side_effect=_table({})):
        assert CpuSampler(7).sample() is None
