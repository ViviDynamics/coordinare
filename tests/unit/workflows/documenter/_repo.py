"""A real temporary git repository with a small wiki for the documenter tests (spec 171)."""
from __future__ import annotations

import subprocess
from pathlib import Path

G = ["git", "-c", "user.email=e@x", "-c", "user.name=t"]


def sh(args, cwd) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


ARCH = """---
kind: explanation
---
# Architecture

## What it is
The service is a small Flask app in `src/app.py` backed by `src/db.py`.

## How it fits
Requests enter `src/app.py`, which calls `src/db.py` for storage.

## Why it is this way
One process keeps the deployment simple; see [setup](setup.md).

## Where to change it
Start from `src/app.py`; the tests in `tests/test_app.py` cover the routes. Keep `src/db.py` free of HTTP concerns.
"""

SETUP = """---
kind: how-to
---
# Set up a development environment

## Goal
Run the app locally with its tests.

## Prerequisites
Python 3.12 and the dependencies in `pyproject.toml`.

## Steps
1. Install dependencies:

```bash
pip install -e .
```

2. Run the tests:

```bash
pytest
```

## Verify
`pytest` reports every test in `tests/` passing.
"""

README = """# demo

> A small Flask demo used by the documenter tests.

## Start here
- [Architecture](architecture.md): how the app is put together.

## Architecture
- [Architecture](architecture.md): how the app is put together.

## How to
- [Set up a development environment](setup.md): run the app and its tests.

## Reference
- Nothing here yet.

## Decisions
- Nothing here yet.

## Optional
- Nothing here yet.
"""


def make_repo(root: Path, *, with_wiki: bool = True, agents_md: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    repo = root / "repo"
    sh(["git", "init", "-q", "-b", "main", str(repo)], root)
    files = {
        "pyproject.toml": '[project]\nname = "demo"\nversion = "0.1.0"\n',
        "src/__init__.py": "",
        "src/app.py": "from flask import Flask\n\napp = Flask(__name__)\n\n\n@app.route('/')\ndef index():\n    return 'ok'\n",
        "src/db.py": "import sqlite3\n\n\ndef connect(path: str):\n    return sqlite3.connect(path)\n",
        "tests/__init__.py": "",
        "tests/test_app.py": "from src.app import app\n\n\ndef test_index():\n    assert app\n",
    }
    if with_wiki:
        files.update({"docs/wiki/README.md": README, "docs/wiki/architecture.md": ARCH, "docs/wiki/setup.md": SETUP})
    if agents_md is not None:
        files["AGENTS.md"] = agents_md
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    sh([*G, "add", "."], repo)
    sh([*G, "commit", "-q", "-m", "base"], repo)
    return repo


def add_payments(repo: Path) -> str:
    """A feature branch adding a payments module; returns the diff against main."""
    sh(["git", "checkout", "-q", "-b", "feat/payments"], repo)
    (repo / "src" / "payments.py").write_text("from src.db import connect\n\n\ndef charge(conn, amount: int) -> bool:\n    conn.execute('INSERT INTO charges VALUES (?)', (amount,))\n    return True\n")
    (repo / "src" / "app.py").write_text((repo / "src" / "app.py").read_text() + "\n\n@app.route('/charge')\ndef charge_route():\n    return 'charged'\n")
    sh([*G, "add", "."], repo)
    sh([*G, "commit", "-q", "-m", "feat: payments"], repo)
    return sh(["git", "diff", "main...HEAD"], repo)


def trivial_change(repo: Path) -> str:
    sh(["git", "checkout", "-q", "-b", "chore/typo"], repo)
    (repo / "src" / "__init__.py").write_text('"""Demo package."""\n')  # nothing in the wiki cites this file
    sh([*G, "add", "."], repo)
    sh([*G, "commit", "-q", "-m", "chore: bump"], repo)
    return sh(["git", "diff", "main...HEAD"], repo)


def head(repo: Path) -> str:
    return sh(["git", "rev-parse", "HEAD"], repo).strip()


def log(repo: Path) -> list[str]:
    return sh(["git", "log", "--format=%s", "-n", "5"], repo).splitlines()


async def local_committer(stand, files, message, deletions=None, *, score=None) -> list[str]:
    """A committer that writes, git-rm's and commits locally (no push): the real one pushes."""
    repo = Path(stand.path)
    changed = []
    for f in files:
        p = repo / f["path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f["content"])
        changed.append(f["path"])
    for d in deletions or []:
        if (repo / d).exists():
            sh([*G, "rm", "-q", d], repo)
            changed.append(d)
    sh([*G, "add", "-A", "--", *[f["path"] for f in files]] if files else [*G, "status", "-s"], repo)
    sh([*G, "commit", "-q", "-m", message], repo)
    return changed
