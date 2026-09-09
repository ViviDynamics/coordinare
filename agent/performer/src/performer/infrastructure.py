"""Infrastructure holds shared by local gates and workflow CI; no repair turn."""
from __future__ import annotations

import re

_PATTERNS = (
    (r"(?:EACCES|EPERM|permission denied)[^\n]*[/]opt/hostedtoolcache",
     "Runner tool-cache permissions prevent CI setup; repair the runner tool cache"),
    (r'login attempt to.*failed with status:\s*(401|403)|(?:registry|docker login|pull access).*?(?:unauthorized|authentication required|403 forbidden|401 unauthorized)',
     'Registry authentication failed; repair CI registry credentials or its authentication proxy'),
    (r'artifact storage.*(?:quota|limit)|storage quota.*(?:exhausted|hit)',
     'Actions storage is exhausted; free storage or raise the Actions budget'),
    (r'no runner came online|runner.*(?:offline|unavailable)|unable to acquire.*runner',
     'CI runner unavailable; restore the runner pool'),
    (r'cannot connect to the docker daemon|permission denied.*docker.sock|no space left on device',
     'Runner container service/storage unavailable; repair the runner'),
)


class InfrastructureBlocked(Exception):
    """An operator/environment hold which never consumes a model repair budget."""


class CIInfrastructureBlocked(InfrastructureBlocked):
    """Remote CI hold, distinguished from the local dependency cache."""

    def __init__(self, reason: str, head_sha: str, check_names: list[str]) -> None:
        super().__init__(reason)
        self.head_sha = head_sha
        self.check_names = check_names


def infrastructure_reason(text: str) -> str | None:
    for pattern, cause in _PATTERNS:
        if re.search(pattern, text, re.IGNORECASE | re.DOTALL):
            return cause
    return None


def check_infrastructure_reason(checks: list[dict]) -> str | None:
    for check in checks:
        if check.get('setup_failure'):
            return 'CI failed during runner/platform setup; repair the runner or platform credentials'
        output = check.get('output') or {}
        text = '\n'.join(str(output.get(key) or '') for key in ('title', 'summary', 'text'))
        reason = infrastructure_reason(text)
        if reason:
            return reason
    if any(check.get('evidence_access_denied') for check in checks):
        return ('Cannot diagnose failed CI because GitHub denied access to failure evidence; '
                'grant the performer Checks and Actions read permissions, then retry CI inspection')
    return None
