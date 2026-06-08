"""Fixture: a Python-specific issue bandit catches (hardcoded secret / weak hash)."""

import hashlib

# CWE-798: hardcoded credential.
API_TOKEN = "AKIAIOSFODNN7EXAMPLE"


def digest(data: bytes) -> str:
    # CWE-327: weak hash (md5).
    return hashlib.md5(data).hexdigest()
