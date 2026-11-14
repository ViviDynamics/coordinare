"""Named native holds without carrying provider or source exception text."""
from __future__ import annotations

from typing import NoReturn

from coordinare.services.environment_attempt import EnvironmentDiscoveryError


def refuse_agent(message: str, *, code: str) -> NoReturn:
    raise EnvironmentDiscoveryError(message, code=code) from None


def invalid_checkpoint(message: str = "Invalid checkpoint") -> NoReturn:
    raise ValueError(message)
