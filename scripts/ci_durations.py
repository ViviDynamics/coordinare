#!/usr/bin/env python
"""Turn the CI shards' JUnit reports into per-file durations (289).

``scripts/ci_shard.py`` balances shards on ``tests/ci-durations.json``. Refresh
it from a CI run's ``junit-*`` artifacts:

    gh run download <run-id> --pattern 'junit-*' --dir /tmp/junit
    python scripts/ci_durations.py /tmp/junit > tests/ci-durations.json

Browser shards run every file twice (chromium and firefox); their times add up,
which is the cost a file places on its shard. Stdlib only.

For several independent runs, pass one directory per run with ``--median``.
Reports within each run still add; repeated observations use their median.
Do not mix plain-test and coverage reports: instrumentation changes their cost.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from statistics import median

REPO_ROOT = Path(__file__).resolve().parent.parent


def file_for(classname: str) -> str | None:
    """Map a JUnit classname such as ``tests.unit.test_x.TestY`` to ``tests/unit/test_x.py``."""
    parts = classname.split(".")
    for end in range(len(parts), 0, -1):
        candidate = Path(*parts[:end]).with_suffix(".py")
        if (REPO_ROOT / candidate).is_file():
            return candidate.as_posix()
    return None


def durations(reports: list[Path]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for report in reports:
        for case in ET.parse(report).iter("testcase"):
            path = file_for(case.get("classname", ""))
            if path is not None:
                totals[path] += float(case.get("time") or 0.0)
    return {path: round(seconds, 2) for path, seconds in sorted(totals.items())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", type=Path, help="JUnit XML files or directories holding them")
    parser.add_argument("--median", action="store_true", help="each path is an independent run; take per-file medians")
    args = parser.parse_args(argv)
    runs = [sorted([p] if p.is_file() else p.rglob("*.xml")) for p in args.paths]
    if args.median:
        samples: dict[str, list[float]] = defaultdict(list)
        for reports in runs:
            for path, seconds in durations(reports).items():
                samples[path].append(seconds)
        result = {path: round(median(values), 2) for path, values in sorted(samples.items())}
    else:
        result = durations(sorted({report for reports in runs for report in reports}))
    json.dump(result, sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
