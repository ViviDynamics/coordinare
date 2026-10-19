"""Inspect an operator-packaged runtime bundle without running project commands."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, field_validator

from .runtime import LINUX_PLATFORM, RuntimeContract
from .runtime_io import check_io, digest_file

if TYPE_CHECKING:
    from threading import Event

BUNDLE_PATH = Path('/opt/coordinare-runtime/bundle.json')


class RuntimeBundleInputs(RuntimeContract):
    schema_version: Literal[1]
    guest_platform: str = Field(pattern=LINUX_PLATFORM)
    guest_image: str
    emulator: str
    firmware: str

    @field_validator('guest_image', 'emulator', 'firmware')
    @classmethod
    def absolute_member(cls, value: str) -> str:
        if not value.startswith('/') or any(part in {'.', '..'} for part in value.split('/')) or '\0' in value:
            raise ValueError('runtime bundle members must be absolute without traversal')
        return value


def read_bundle(metadata: Path, *, deadline: float | None = None, stop: Event | None = None) -> RuntimeBundleInputs:
    """Bound metadata before parsing and retain the caller's inspection budget."""
    check_io(deadline, stop)
    with metadata.open('rb') as stream:
        payload = stream.read(16 * 1024 + 1)
    if len(payload) > 16 * 1024:
        raise ValueError('runtime bundle metadata exceeds limit')
    check_io(deadline, stop)
    return RuntimeBundleInputs.model_validate_json(payload)


def inspect_bundle(metadata: Path = BUNDLE_PATH, *, deadline: float | None = None, stop: Event | None = None) -> dict[str, str]:
    """Hash actual image members, never trust stored digest declarations."""
    bundle = read_bundle(metadata, deadline=deadline, stop=stop)
    result = {'guest_platform': bundle.guest_platform, 'configuration_sha256': bundle.digest()}
    for key, name in [('guest_image', 'guest_base_sha256'), ('emulator', 'emulator_sha256'), ('firmware', 'firmware_sha256')]:
        path = Path(getattr(bundle, key)).resolve(strict=True)
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError('runtime bundle member must be a nonempty regular file')
        result[name] = digest_file(path, deadline=deadline, stop=stop)
    return result


def main() -> int:
    try:
        result = inspect_bundle()
    except (ValueError, OSError):
        sys.stderr.write('runtime bundle unavailable or malformed\n')
        return 1
    sys.stdout.write(json.dumps(result, sort_keys=True) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
