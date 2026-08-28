#!/usr/bin/env python3
"""Consolidate browser-matrix run output into a committed, durable artifact (077).

The live matrix writes per-model dirs under tmp/ (gitignored, ephemeral). This
scans every completed run and emits a small, queryable, git-committed snapshot:

  specs/077-multi-backend-qa/matrix_results.json  — one row per (model, backend)
  specs/077-multi-backend-qa/matrix_results.md    — the pivoted PASS/FAIL grid

Idempotent and re-runnable: run it any time to refresh from whatever has
completed so far, so a crash or a `tmp/` wipe never loses more than the single
in-flight model. Sources scanned: tmp/model_runs/*/matrix.log,
tmp/qwen_runs/*/matrix.log, and the two pre-archiving gpt-oss logs in /tmp.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_backends import REPO_ROOT

OUT_DIR = REPO_ROOT / "specs" / "077-multi-backend-qa"
ROW_RE = re.compile(
    r"^(?P<endpoint>\S+-ephemeral)\s+(?P<backend>\S+)\s+(?P<launch>yes|NO)\s+"
    r"(?P<state>\S+)\s+(?P<kb>\d+)\s+(?P<png>yes|-)\s")
MODEL_RE = re.compile(r"model=([^)]+)\)")

# Extra sources that predate the per-model archive dirs (logs only).
EXTRA_LOGS = [
    ("gpt-oss:120b", Path("/tmp/shot_fair_matrix.log")),
    ("gpt-oss:20b", Path("/tmp/shot_20b_matrix.log")),
]


def _note_from_json(run_dir: Path, endpoint: str) -> str:
    """Pull a short human note (the agent's reported progress / error) from the
    archived per-cell job result, when present."""
    p = run_dir / f"{endpoint}.json"
    if not p.is_file():
        return ""
    try:
        d = json.loads(p.read_text())
        res = d.get("result") or {}
        summ = res.get("summary") or ""
        m = re.search(r'"progress":"(.*?)","', summ, re.S)
        note = (m.group(1) if m else (res.get("error_code") or "")).strip()
        return note.replace("\\n", " ").replace("\\r", " ")[:140]
    except Exception:
        return ""


def _parse_log(model: str | None, log: Path, run_dir: Path | None) -> list[dict]:
    text = log.read_text(errors="replace")
    if model is None:
        m = MODEL_RE.search(text)
        model = m.group(1) if m else log.parent.name
    # Strip a leading "local/" so the same logical model collapses across routes.
    model_key = model.replace("local/", "")
    rows = []
    for line in text.splitlines():
        m = ROW_RE.match(line)
        if not m:
            continue
        g = m.groupdict()
        note = _note_from_json(run_dir, g["endpoint"]) if run_dir else ""
        rows.append({
            "model": model_key,
            "backend": g["backend"],
            "endpoint": g["endpoint"],
            "launched": g["launch"] == "yes",
            "state": g["state"],
            "shot_kb": int(g["kb"]),
            "pass": g["png"] == "yes",
            "note": note,
        })
    return rows


def main() -> int:
    cells: list[dict] = []
    for base in (REPO_ROOT / "tmp" / "model_runs", REPO_ROOT / "tmp" / "qwen_runs"):
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            log = d / "matrix.log"
            if log.is_file():
                cells += _parse_log(None, log, d)
    for model, log in EXTRA_LOGS:
        if log.is_file():
            cells += _parse_log(model, log, None)

    # De-dup (model, endpoint): keep the last occurrence.
    seen: dict[tuple[str, str], dict] = {}
    for c in cells:
        seen[(c["model"], c["endpoint"])] = c
    cells = sorted(seen.values(), key=lambda c: (c["model"], c["endpoint"]))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "matrix_results.json").write_text(json.dumps(cells, indent=2))

    # Pivot to a model x backend grid.
    models = sorted({c["model"] for c in cells})
    backends = sorted({c["backend"] for c in cells})
    grid = {(c["model"], c["backend"]): c for c in cells}
    lines = ["# Browser-control matrix — consolidated results", "",
             f"_{len(cells)} cells, {len(models)} models x {len(backends)} backends. "
             "✅ = drove Chrome → valid PNG; ❌ = no valid PNG; - = not run._", "",
             "| model | " + " | ".join(backends) + " | pass |",
             "|---|" + "|".join("---" for _ in backends) + "|---|"]
    for model in models:
        row, npass, ntot = [], 0, 0
        for b in backends:
            c = grid.get((model, b))
            if not c:
                row.append("-")
                continue
            ntot += 1
            if c["pass"]:
                npass += 1
                row.append("✅")
            else:
                row.append("❌")
        lines.append(f"| `{model}` | " + " | ".join(row) + f" | {npass}/{ntot} |")
    # Per-backend totals.
    lines += ["", "## Per-backend (valid PNG across models)"]
    for b in backends:
        bc = [c for c in cells if c["backend"] == b]
        lines.append(f"- **{b}**: {sum(c['pass'] for c in bc)}/{len(bc)}")
    (OUT_DIR / "matrix_results.md").write_text("\n".join(lines) + "\n")

    print(f"Consolidated {len(cells)} cells across {len(models)} models.")
    print(f"  -> {OUT_DIR / 'matrix_results.json'}")
    print(f"  -> {OUT_DIR / 'matrix_results.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
