from __future__ import annotations

from enum import StrEnum


class Status(StrEnum):
    OK = "ok"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"
