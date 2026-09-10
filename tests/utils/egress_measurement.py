"""Independent live-test evidence: failed reachability is not policy proof."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectionObservation:
    returncode: int
    stdout: str

    @property
    def outcome(self) -> str:
        lines = self.stdout.splitlines()
        if self.returncode == 0 and 'ATTEMPTED' in lines and 'REACHED' in lines:
            return 'reached'
        if self.returncode == 1 and 'ATTEMPTED' in lines and 'CONNECT_FAILED' in lines:
            return 'failed'
        return 'inconclusive'


def compare_observations(before, restricted, after) -> bool | None:
    """Only repeatable failures bracketed by working controls indicate blocking."""
    if before.outcome != 'reached' or after.outcome != 'reached':
        return None
    outcomes = [item.outcome for item in restricted]
    if len(outcomes) < 2 or any(item == 'inconclusive' for item in outcomes):
        return None
    if all(item == 'failed' for item in outcomes):
        return True
    if all(item == 'reached' for item in outcomes):
        return False
    return None
