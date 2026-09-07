"""Fixture pull requests for the security workflow eval (spec 170 SC-006).

Each fixture carries the diff, the fake scanner output per tool, the canned
model replies in call order (survey proposal, optional coverage proposal,
findings, optional re-anchor), and the expectations scoring checks.
Deterministic by construction; the live mode swaps the stub model and the fake
scanner for the gateway and the real tools over a temporary repository and
keeps the expectations loose where a real model varies.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DIFF = """diff --git a/src/db.py b/src/db.py
index 1111111..2222222 100644
--- a/src/db.py
+++ b/src/db.py
@@ -1,4 +1,9 @@
 import sqlite3
 from flask import request
+
+
+def lookup(conn):
+    user_id = request.args["id"]
+    query = "SELECT * FROM users WHERE id = " + user_id
+    return conn.execute(query).fetchall()
"""

CLEAN_DIFF = """diff --git a/src/db.py b/src/db.py
index 1111111..2222222 100644
--- a/src/db.py
+++ b/src/db.py
@@ -1,4 +1,8 @@
 import sqlite3
 from flask import request
+
+
+def lookup(conn, user_id: int):
+    return conn.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchall()
"""

SECRET_DIFF = """diff --git a/src/config.py b/src/config.py
index 1111111..2222222 100644
--- a/src/config.py
+++ b/src/config.py
@@ -1,2 +1,4 @@
 import os
+
+API_KEY = "sk-live-0123456789abcdef0123456789abcdef"
"""

HASH_DIFF = """diff --git a/src/tokens.py b/src/tokens.py
index 1111111..2222222 100644
--- a/src/tokens.py
+++ b/src/tokens.py
@@ -1,2 +1,6 @@
 import hashlib
+
+
+def fingerprint(token: str) -> str:
+    return hashlib.md5(token.encode()).hexdigest()
"""

SURVEY = {"commands": [{"command": "git log --oneline -3", "reason": "recent history"}]}
NO_FINDINGS = {"findings": []}
INJECTION = {"path": "src/db.py", "line": 7, "category": "injection", "problem": "request parameter concatenated into SQL",
             "why_blocking": "an attacker controls the query text", "evidence": 'query = "SELECT * FROM users WHERE id = " + user_id',
             "introduced_by": "src/db.py", "downgrade_reason": ""}
WEAK_HASH = {"path": "src/tokens.py", "line": 6, "category": "weak_crypto", "problem": "MD5 used to fingerprint a secret token",
             "why_blocking": "MD5 is broken for anything security relevant", "evidence": "return hashlib.md5(token.encode()).hexdigest()",
             "introduced_by": "src/tokens.py", "downgrade_reason": ""}
SEMGREP_KEY = {"check_id": "generic.secrets.hardcoded-key", "path": "src/config.py", "start": {"line": 3},
               "extra": {"severity": "ERROR", "metadata": {"cwe": ["CWE-798: Use of Hard-coded Credentials"]}}}


@dataclass(frozen=True)
class Expectation:
    verdict: str
    min_blocking: int
    max_blocking: int
    review_event: str | None            # REQUEST_CHANGES, COMMENT, or None when nothing is posted
    categories: tuple[str, ...] = ()    # every one must appear among blocking + advisory
    tools: tuple[str, ...] = ()         # every one must appear among the surviving findings' tools
    downgraded: int = 0
    hold_names: str = ""                # substring the hold reason must carry
    live_verdicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Fixture:
    name: str
    diff: str
    files: dict[str, str]               # repository contents for the live temporary repo
    replies: list[dict]
    expect: Expectation
    semgrep: list[dict] = field(default_factory=list)
    bandit: list[dict] = field(default_factory=list)
    scanner_failure: str | None = None  # tool name whose binary is missing in the fake runner
    brief: dict = field(default_factory=lambda: {"work_kind": "feature"})


CLEAN = Fixture(
    name="clean", diff=CLEAN_DIFF, replies=[SURVEY, NO_FINDINGS],
    files={"src/db.py": 'import sqlite3\nfrom flask import request\n\n\ndef lookup(conn, user_id: int):\n    return conn.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchall()\n'},
    expect=Expectation(verdict="security_passed", min_blocking=0, max_blocking=0, review_event="COMMENT", live_verdicts=("security_passed", "security_failed")),
)
INJECTION_FIXTURE = Fixture(
    name="injection", diff=DIFF, replies=[SURVEY, {"findings": [INJECTION]}],
    files={"src/db.py": 'import sqlite3\nfrom flask import request\n\n\ndef lookup(conn):\n    user_id = request.args["id"]\n    query = "SELECT * FROM users WHERE id = " + user_id\n    return conn.execute(query).fetchall()\n'},
    expect=Expectation(verdict="security_failed", min_blocking=1, max_blocking=60, review_event="REQUEST_CHANGES", categories=("injection",), live_verdicts=("security_failed",)),
)
SECRET = Fixture(
    name="secret", diff=SECRET_DIFF, replies=[SURVEY, NO_FINDINGS], semgrep=[SEMGREP_KEY],
    files={"src/config.py": 'import os\n\nAPI_KEY = "sk-live-0123456789abcdef0123456789abcdef"\n'},
    expect=Expectation(verdict="security_failed", min_blocking=1, max_blocking=60, review_event="REQUEST_CHANGES", categories=("hardcoded_secret",), tools=("semgrep",), live_verdicts=("security_failed",)),
)
SCANNER_UNAVAILABLE = Fixture(
    name="scanner_unavailable", diff=DIFF, replies=[SURVEY, {"findings": [INJECTION]}], scanner_failure="semgrep",
    files={"src/db.py": "x = 1\n"},
    expect=Expectation(verdict="env_blocked", min_blocking=0, max_blocking=0, review_event=None, hold_names="semgrep", live_verdicts=("env_blocked",)),
)
ADVISORY_ONLY = Fixture(
    name="advisory_only", diff=HASH_DIFF, replies=[SURVEY, {"findings": [WEAK_HASH]}],
    files={"src/tokens.py": "import hashlib\n\n\ndef fingerprint(token: str) -> str:\n    return hashlib.md5(token.encode()).hexdigest()\n"},
    expect=Expectation(verdict="security_passed", min_blocking=0, max_blocking=0, review_event="COMMENT", categories=("weak_crypto",), live_verdicts=("security_passed", "security_failed")),
)
DOWNGRADE = Fixture(
    name="downgrade", diff=DIFF, replies=[SURVEY, {"findings": [{**INJECTION, "downgrade_reason": "user_id is validated as an integer by the route decorator"}]}],
    files={"src/db.py": "x = 1\n"},
    expect=Expectation(verdict="security_passed", min_blocking=0, max_blocking=0, review_event="COMMENT", categories=("injection",), downgraded=1, live_verdicts=("security_passed", "security_failed")),
)

FIXTURES: list[Fixture] = [CLEAN, INJECTION_FIXTURE, SECRET, SCANNER_UNAVAILABLE, ADVISORY_ONLY, DOWNGRADE]
