"""Read transient runtime controls before launching any project commands."""
from __future__ import annotations

import math
import os
import time

from .runtime import RuntimeArtifactReceipt


def consume_runtime_controls(*, required: bool, install: bool) -> tuple[float | None, RuntimeArtifactReceipt | None]:
    expiry = os.environ.pop('COORDINARE_ENV_RUNTIME_EXPIRES_AT', None)
    encoded = os.environ.pop('COORDINARE_ENV_RUNTIME_ARTIFACTS', None)
    if not required:
        if expiry is not None or encoded is not None:
            raise ValueError('native manifest cannot use private runtime controls')
        return None, None
    if expiry is None or len(expiry) > 64:
        raise ValueError('private runtime requires original attempt expiry')
    try:
        expires_at = float(expiry)
    except ValueError:
        raise ValueError('private runtime attempt expiry invalid') from None
    if not math.isfinite(expires_at):
        raise ValueError('private runtime attempt expiry invalid')
    remaining = expires_at-time.time()
    if remaining <= 0:
        raise TimeoutError('private runtime original attempt expired')
    receipt = None
    if encoded is not None:
        if len(encoded.encode()) > 64*1024:
            raise ValueError('private runtime receipt exceeds transfer bound')
        try:
            receipt = RuntimeArtifactReceipt.model_validate_json(encoded)
        except ValueError:
            raise ValueError('private runtime receipt invalid') from None
    if install and receipt is not None:
        raise ValueError('private runtime installer cannot reuse a consumer receipt')
    if not install and receipt is None:
        raise ValueError('private runtime consumer requires sealed receipt')
    return time.monotonic()+remaining, receipt
