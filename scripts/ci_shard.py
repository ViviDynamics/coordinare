#!/usr/bin/env python
"""Split test files into CI shards (289).

Prints the test files for one shard, one per line. Every file under the roots
lands in exactly one shard, and the split is deterministic, so parallel jobs
together run the same tests one unsharded run would.

Files are weighted by measured seconds from ``tests/ci-durations.json`` when a
file is listed there, and otherwise by how many tests it defines. The heaviest
file goes to the lightest shard first.

Stdlib only:

    python scripts/ci_shard.py --shard 1 --of 4 tests --exclude tests/e2e
    python scripts/ci_shard.py --shard 2 --of 3 tests/e2e
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DURATIONS = REPO_ROOT / "tests" / "ci-durations.json"
# Pytest's default discovery patterns; pyproject.toml does not override them.
_TEST_FILE = re.compile(r"^(test_.*|.*_test)\.py$")
_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+test", re.MULTILINE)


def discover_test_files(roots: list[Path], exclude: list[Path] | None = None) -> list[Path]:
    """Every pytest-discoverable file under the roots and outside ``exclude``, relative to the repo, sorted."""
    skipped = [(p if p.is_absolute() else REPO_ROOT / p).resolve() for p in exclude or []]
    found: set[Path] = set()
    for root in roots:
        base = root if root.is_absolute() else REPO_ROOT / root
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts or not _TEST_FILE.match(path.name):
                continue
            if any(path.resolve().is_relative_to(dropped) for dropped in skipped):
                continue
            found.add(path.relative_to(REPO_ROOT))
    return sorted(found)


def weight(path: Path, durations: dict[str, float]) -> float:
    measured = durations.get(path.as_posix())
    if measured is not None:
        return max(float(measured), 0.01)
    text = (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
    return float(max(len(_TEST_DEF.findall(text)), 1))


def assign(files: list[Path], shards: int, durations: dict[str, float]) -> list[list[Path]]:
    """Longest-first greedy assignment; ties break on path, then shard index."""
    buckets: list[list[Path]] = [[] for _ in range(shards)]
    loads = [0.0] * shards
    for path in sorted(files, key=lambda p: (-weight(p, durations), p.as_posix())):
        lightest = min(range(shards), key=lambda i: (loads[i], i))
        buckets[lightest].append(path)
        loads[lightest] += weight(path, durations)
    return [sorted(bucket) for bucket in buckets]


def load_durations(path: Path) -> dict[str, float]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(key): float(value) for key, value in data.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shard", type=int, required=True, help="1-based shard number")
    parser.add_argument("--of", type=int, required=True, help="total number of shards")
    parser.add_argument("--durations", type=Path, default=DEFAULT_DURATIONS)
    parser.add_argument(
        "--exclude", action="append", type=Path, default=[],
        help="a directory another lane runs; its files join no shard here",
    )
    parser.add_argument("roots", nargs="+", type=Path)
    args = parser.parse_args(argv)
    if args.of < 1 or not 1 <= args.shard <= args.of:
        parser.error(f"--shard must be between 1 and --of ({args.of})")
    files = discover_test_files(args.roots, args.exclude)
    if not files:
        parser.error("no test files found under " + ", ".join(map(str, args.roots)))
    bucket = assign(files, args.of, load_durations(args.durations))[args.shard - 1]
    sys.stdout.write("".join(f"{path.as_posix()}\n" for path in bucket))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
