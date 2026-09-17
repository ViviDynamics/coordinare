#!/usr/bin/env python
"""Ruff violation ratchet (432).

Compares current per-rule violation counts for the debt families against a
committed baseline JSON. Any increase fails. Any decrease passes (the baseline
can then be lowered with --update; a rule that reaches zero moves into the
normal ruff select list).

Scoped to production roots (src/, agent/performer/src): tests use assert and
print idiomatically, so a ratchet over tests/ would block every PR that adds
coverage instead of freezing production debt.

Stdlib only. Run under the project venv so `sys.executable -m ruff` resolves:

    uv run --no-sync python scripts/ruff_baseline.py            # check
    uv run --no-sync python scripts/ruff_baseline.py --update   # lower the bar
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = Path(__file__).resolve().with_name("ruff-baseline.json")
DEBT_RULES = ["PERF401", "PLR", "TRY", "ASYNC", "S", "PTH", "T20", "PLC"]
ROOTS = ["src", "agent/performer/src"]


def current_counts() -> dict[str, int]:
    """Run ruff over the debt families and count violations per rule code."""
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache",
         "--select", ",".join(DEBT_RULES), "--output-format", "json", *ROOTS],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    # ruff exits 0 when clean and 1 when diagnostics were produced; anything
    # else (2+) is an invocation failure worth dying on.
    if proc.returncode not in (0, 1):
        raise SystemExit(f"ruff failed ({proc.returncode}): {proc.stderr}")
    diagnostics = json.loads(proc.stdout)
    counts: dict[str, int] = {}
    for item in diagnostics:
        code = item.get("code") or ""
        if code:
            counts[code] = counts.get(code, 0) + 1
    return counts


def compare(
    baseline: dict[str, int], counts: dict[str, int]
) -> tuple[list[str], list[str]]:
    """Return (increases, decreases) rule lines for the current counts."""
    increases = []
    decreases = []
    for code, now in sorted(counts.items()):
        allowed = baseline.get(code, 0)
        if now > allowed:
            increases.append(f"{code}: {allowed} -> {now}")
        elif now < allowed:
            decreases.append(f"{code}: {allowed} -> {now}")
    return increases, decreases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline", type=Path, default=DEFAULT_BASELINE,
        help="Path to the committed baseline JSON",
    )
    parser.add_argument(
        "--update", action="store_true",
        help="Lower (or write) the baseline to the current counts and exit",
    )
    args = parser.parse_args()

    counts = current_counts()
    if args.update:
        payload = {
            "comment": "432 ratchet: per-rule ruff violation counts; decreases only",
            "rules": dict(sorted(counts.items())),
        }
        args.baseline.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"baseline written: {args.baseline} ({sum(counts.values())} violations)")
        return 0

    baseline_doc = json.loads(args.baseline.read_text())
    baseline: dict[str, int] = baseline_doc["rules"]
    increases, decreases = compare(baseline, counts)

    for line in decreases:
        print(f"lowered: {line}")
    for line in increases:
        print(f"increased: {line}")
    if increases:
        print(f"\n{len(increases)} rule(s) increased; fix the violations or lower no baseline.", file=sys.stderr)
        return 1
    print(f"baseline holds ({sum(counts.values())} violations across {len(counts)} rules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
