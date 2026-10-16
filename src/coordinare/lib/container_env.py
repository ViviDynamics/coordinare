from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def docker_environment(values: dict[str, str]) -> Iterator[Path | None]:
    """Keep runtime credentials out of argv; remove the private file on every exit."""
    if not values:
        yield None
        return
    if any(not key.isidentifier() or any(char in value for char in "\r\n\0") for key, value in values.items()):
        message = "Docker environment requires valid names and single-line values."
        raise ValueError(message)
    # NamedTemporaryFile creates the file with mode 0600, including during writes.
    with tempfile.NamedTemporaryFile(mode="w", prefix="coordinare-container-", suffix=".env") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")
        handle.flush()
        yield Path(handle.name)
