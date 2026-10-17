"""Trusted environment attempt failures and resource accounting."""
from __future__ import annotations


class EnvironmentDiscoveryError(RuntimeError):
    """Discovery cannot establish an accepted, evidence-bound environment."""
