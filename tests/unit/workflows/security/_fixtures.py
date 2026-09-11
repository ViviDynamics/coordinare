"""Shared fixtures for the security workflow tests (spec 170)."""
from __future__ import annotations

import json

from performer.workflows.reviewer.models import ChangedFile, Hunk
from performer.workflows.security.models import SecurityFinding

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
diff --git a/tests/test_db.py b/tests/test_db.py
index 3333333..4444444 100644
--- a/tests/test_db.py
+++ b/tests/test_db.py
@@ -1,2 +1,5 @@
 from src.db import lookup
+
+
+def test_lookup_exists():
+    assert lookup
"""

TRUNCATED_DIFF = DIFF + "diff --git a/src/extra.py b/src/extra.py\n--- a/src/extra.py\n+++ b/src/extra.py\n@@ -1,2 +1,3 @@\n import os\n+X = 1\n\n[coordinare: diff truncated to 60000 chars; run `gh pr diff <pr_url>` for the full changes]\n"

SURVEY = {"commands": [{"command": "git log --oneline -3", "reason": "recent history"}]}
OPEN_SINK = {"commands": [{"command": "sed -n '1,40p' src/sink.py", "reason": "read the sink"}]}
OPEN_EXTRA = {"commands": [{"command": "sed -n '1,40p' src/extra.py", "reason": "read the cut file"}]}
NO_FINDINGS = {"findings": []}

INJECTION = {"path": "src/db.py", "line": 7, "category": "injection", "problem": "request parameter concatenated into SQL",
             "why_blocking": "an attacker controls the query text", "evidence": 'query = "SELECT * FROM users WHERE id = " + user_id',
             "introduced_by": "src/db.py", "downgrade_reason": ""}

# 366: a scanner reading as it now arrives at the gate. The tool is stamped by
# the runner that ran it, and the category is what the model reading the
# output called it -- not a CWE number ("798") or a rule word ("hashlib") for
# coordinare to look up in a table it no longer has. Severity and routing are
# absent on purpose: they come from the category, for scanner and model
# findings alike, so a reading cannot call a SQL injection low.
SEMGREP_SECRET = {"tool": "semgrep", "category": "hardcoded_secret", "description": "generic.secrets.hardcoded-key", "file": "src/db.py", "line": 6}
BANDIT_MD5 = {"tool": "bandit", "category": "weak_crypto", "description": "B324", "file": "src/db.py", "line": 8}


def changed_files():
    return [
        ChangedFile(path="src/db.py", hunks=[Hunk(header="@@ -1,4 +1,9 @@", start_line=1, end_line=9, lines=[])], fully_in_diff=True),
        ChangedFile(path="tests/test_db.py", hunks=[Hunk(header="@@ -1,2 +1,5 @@", start_line=1, end_line=5, lines=[])], fully_in_diff=True),
    ]


def finding(**over) -> SecurityFinding:
    base = dict(path="src/db.py", line=7, category="injection", problem="request parameter concatenated into SQL", why_blocking="attacker controls the query",
                evidence='query = "SELECT * FROM users WHERE id = " + user_id', origin="model", severity="high", routing="implementer",
                introduced_by="src/db.py", tool="model")
    base.update(over)
    return SecurityFinding(**base)


def semgrep_json(results: list[dict]) -> str:
    return json.dumps({"results": results, "errors": []})


def bandit_json(results: list[dict]) -> str:
    return json.dumps({"results": results, "errors": []})
