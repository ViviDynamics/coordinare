"""Measured Linux carrier controls for the explicitly enabled guest adapter."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import RuntimeResources


def _read(path: str) -> str:
    text = Path(path).read_text()
    if len(text) > 16384:
        message = 'kernel control exceeds measurement bound'
        raise ValueError(message)
    return text.strip()


def require_runtime_resources(resources: RuntimeResources) -> None:
    """Refuse unknown limits: v2 cgroups and dropped privileges are required."""
    try:
        quota, period = _read('/sys/fs/cgroup/cpu.max').split()
        cpu_matches = int(period) > 0 and int(quota) == resources.cpus*int(period)
        memory_matches = int(_read('/sys/fs/cgroup/memory.max')) == resources.memory_mib*1024**2
        swap_disabled = int(_read('/sys/fs/cgroup/memory.swap.max')) == 0
        pids = int(_read('/sys/fs/cgroup/pids.max'))
        status = dict(line.split(':', 1) for line in _read('/proc/self/status').splitlines() if ':' in line)
        caps_dropped = all(int(status[key].strip(), 16) == 0 for key in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'))
        no_gain = status['NoNewPrivs'].strip() == '1'
        accepted = cpu_matches and memory_matches and swap_disabled and 0 < pids <= 512 and caps_dropped and no_gain
    except (OSError, ValueError, KeyError):
        message = 'private runtime kernel resource or privilege controls are unavailable'
        raise ValueError(message) from None
    if not accepted:
        message = 'private runtime kernel resource or privilege controls differ'
        raise ValueError(message)
