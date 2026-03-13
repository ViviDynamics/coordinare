"""
Contract: BackendEvent secret redaction.

This is the only remaining implementation contract for 014.
All other contracts are already satisfied by existing code.

Branch: 014-performer-event-stream
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Secret patterns — ordered by specificity (most specific first)
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # GitHub fine-grained PAT (longest, most specific)
    re.compile(r"github_pat_[A-Za-z0-9_]{82,}"),
    # GitHub classic PAT
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    # GitHub OAuth app token
    re.compile(r"gho_[A-Za-z0-9]{36}"),
    # GitHub App installation token
    re.compile(r"ghs_[A-Za-z0-9]{36}"),
    # Anthropic API key
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{90,}"),
    # Generic Bearer token (Authorization header value)
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{20,}"),
    # AWS access key
    re.compile(r"AKIA[0-9A-Z]{16}"),
]

_REPLACEMENT = "[REDACTED]"


def redact_secrets(text: str) -> str:
    """Replace any known secret patterns in *text* with '[REDACTED]'.

    This function is:
    - Pure (no I/O, no side effects)
    - Idempotent (calling twice yields the same result)
    - Conservative: only replaces strings that match a specific known format;
      generic "looks long and random" heuristics are NOT used

    Args:
        text: The string to redact (typically BackendEvent.detail)

    Returns:
        The input string with any matched secrets replaced by '[REDACTED]'.
    """
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REPLACEMENT, text)
    return text


# ---------------------------------------------------------------------------
# Integration point: BackendEvent model_validator
# ---------------------------------------------------------------------------
#
# In performer/models.py, add:
#
#   from pydantic import model_validator
#
#   class BackendEvent(BaseModel):
#       ...
#
#       @model_validator(mode="before")
#       @classmethod
#       def _redact_sensitive_detail(cls, data: dict) -> dict:
#           if isinstance(data, dict) and "detail" in data:
#               data["detail"] = redact_secrets(str(data["detail"]))
#           return data
#
# ---------------------------------------------------------------------------
