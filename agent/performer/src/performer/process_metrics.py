"""237: CPU for the performer's process tree, measured between two polls.

psutil's ``cpu_percent(interval=None)`` measures the time since the previous call on the
same ``Process`` object. The first call on a new object returns 0.0 without measuring
anything, so a sampler that builds a fresh object each poll reports 0.0 forever. The
sampler keeps one object per pid instead, and a process is measured only from its second
sample on.

It sums the performer and every descendant. The agent runs as a child process, and the
performer's own CPU stays near zero while the agent works, so reading the performer alone
is what made the dashboard show 0.0% during a run.
"""
from __future__ import annotations

import psutil


class CpuSampler:
    """Percent CPU of one process tree, since the previous :meth:`sample` call."""

    def __init__(self, root_pid: int) -> None:
        self._root_pid = root_pid
        self._processes: dict[int, psutil.Process] = {}

    def sample(self) -> float | None:
        """The tree's CPU percent since the last sample, or None when nothing was measurable yet.

        None means no process in the tree has been sampled before, so there is no interval to
        report. A process that appeared since the last sample is primed here and counts from the
        next one. A process that exited is dropped from the tree without error.
        """
        try:
            root = psutil.Process(self._root_pid)
            found = [root, *root.children(recursive=True)]
        except psutil.Error:
            self._processes = {}
            return None
        total = 0.0
        measured = False
        current: dict[int, psutil.Process] = {}
        for proc in found:
            try:
                known = self._processes.get(proc.pid)
                if known is None or known.create_time() != proc.create_time():
                    # A new process, or a pid the OS reused: this call only primes its counter.
                    proc.cpu_percent(interval=None)
                    current[proc.pid] = proc
                    continue
                current[proc.pid] = known
                total += known.cpu_percent(interval=None)
                measured = True
            except psutil.Error:
                continue
        self._processes = current
        return total if measured else None
