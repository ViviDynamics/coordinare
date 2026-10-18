"""Cycle-free repository path and credential guards for shared evidence."""
from __future__ import annotations

import re
from pathlib import PurePosixPath


def source_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value == "."
        or "\0" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or "\\" in value
    ):
        raise ValueError("source must be a relative repository path without traversal")
    return value



def reject_literal_credentials(value: str) -> None:
    """Credential-bearing URLs are runtime references, never persisted command data."""
    if "://" not in value:
        return
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/@]+:([^\s/@]+)@", value):
        if not re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", match.group(1)):
            raise ValueError("credentials must use a named runtime reference")


def reject_nested_credentials(value: object) -> None:
    if isinstance(value, str):
        reject_literal_credentials(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            reject_nested_credentials(key)
            reject_nested_credentials(item)
    elif isinstance(value, list):
        for item in value:
            reject_nested_credentials(item)
