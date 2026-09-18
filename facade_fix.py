"""Post-processing for the generated monitor_performer.py facade (435 batch 3).

Run after gen_batch3.py. Applies:
  1. logger: drop `import structlog` + `logger = structlog.get_logger(__name__)`,
     import the shared logger from monitor.body instead (tests patch
     monitor_performer.logger; sharing the object keeps the patch effective).
  2. Re-export surface: names in REQUIRED must stay importable from this module
     (consumed by tests and other src modules). monitor.* import blocks are
     rebuilt with node-used names live and REQUIRED names noqa'd; everything
     else is left for ruff --fix to delete.
"""

import ast
import subprocess
from pathlib import Path

FACADE = Path("src/coordinare/graph/nodes/monitor_performer.py")
MONITOR_PKG = "coordinare.graph.nodes.monitor."

# names that MUST remain importable from monitor_performer (consumed elsewhere)
REQUIRED = [
    "ABSOLUTE_CEILING_MULTIPLIER",
    "EXPECTED_STAGE_MARKER",
    "MAX_PERFORMER_EVENTS",
    "TERMINAL_SUCCESS_STATES",
    "VERDICT_STAGES",
    "merge_performer_events",
    "_recent_event_text",
    "_FEEDBACK_DIGEST_MAX",
    "_advance_stage",
    "_apply_assessor_decline",
    "_apply_feedback_dispositions",
    "_apply_pending_override",
    "_build_baseline_index",
    "_evaluate_baseline_prevention_gate",
    "_evaluate_pr_checks_gate",
    "_feedback_cycle_exhausted",
    "_lift_review_findings",
    "_lift_security_findings",
    "_record_activity_batch",
    "_record_pr_artefacts",
    "_record_stage_verdict",
    "_reset_ci_gate_api_error_cooldown",
    "_resolve_dispute_round",
    "_stamp_feedback_bounce",
    "_summarise_feedback_items",
    "_teardown_workspace",
    "_get_ci_gate_config",
    "_refresh_backend_ui",
    "logger",
]


def import_map(src: str) -> dict[str, str]:
    """Map each imported name from monitor.* blocks to its source module."""
    mapping: dict[str, str] = {}
    for stmt in ast.parse(src).body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module and stmt.module.startswith(MONITOR_PKG):
            module = stmt.module[len(MONITOR_PKG):]
            for alias in stmt.names:
                mapping[alias.asname or alias.name] = module
    return mapping


def node_used_names(src: str) -> set[str]:
    """Names referenced inside the facade's own functions (the node)."""
    used: set[str] = set()
    for stmt in ast.parse(src).body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                    used.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    root: ast.AST = sub
                    while isinstance(root, ast.Attribute):
                        root = root.value
                    if isinstance(root, ast.Name):
                        used.add(root.id)
    return used


def extract_module(stripped: str) -> str:
    assert stripped.startswith(f"from {MONITOR_PKG}"), stripped
    rest = stripped[len(f"from {MONITOR_PKG}"):]
    return rest[: rest.index(" import")].strip()


def rebuild(src: str) -> str:
    name_module = import_map(src)
    used = node_used_names(src)

    missing = [n for n in REQUIRED if n not in name_module]
    if missing:
        raise SystemExit(f"REQUIRED names not found in generated facade: {missing}")

    by_module: dict[str, list[tuple[str, bool]]] = {}  # (name, noqa)
    for name in REQUIRED:
        by_module.setdefault(name_module[name], []).append((name, True))
    for name, module in name_module.items():
        if name in used and name not in REQUIRED:
            by_module.setdefault(module, []).append((name, False))

    lines = src.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped.startswith(f"from {MONITOR_PKG}"):
            out.append(lines[i])
            i += 1
            continue
        # a monitor.* import: parenthesized block or single line
        if "(" in stripped:
            module = extract_module(stripped[: stripped.index("(")])
            j = i
            while not lines[j].strip().startswith(")"):
                j += 1
            i = j + 1
        else:
            module = extract_module(stripped)
            i += 1
        names = by_module.pop(module, None)
        if names is None:
            continue  # block is entirely deletable re-exports
        block = [f"from {MONITOR_PKG}{module} import (\n"]
        block.extend(
            f"    {name},  # noqa: F401 -- re-export\n" if noqa else f"    {name},\n"
            for name, noqa in names
        )
        block.append(")\n")
        out.extend(block)
    return "".join(out)


def main() -> None:
    src = FACADE.read_text()

    # 1. logger from body (idempotent: skip if the body import already carries it)
    src = src.replace("import structlog\n", "")
    src = src.replace("logger = structlog.get_logger(__name__)\n\n", "")
    bare_body_import = f"from {MONITOR_PKG}body import _monitor_performer_body\n"
    if bare_body_import in src:
        src = src.replace(
            bare_body_import,
            f"from {MONITOR_PKG}body import _monitor_performer_body, logger\n",
        )

    # 2. rebuild monitor.* blocks: node-used names live, REQUIRED noqa'd
    src = rebuild(src)

    # 3. explicit re-export surface: __all__ satisfies mypy's no_implicit_reexport
    #    for src consumers that access these names on the module
    #    (idempotent: drop any __all__ previously appended by this script)
    all_start = src.find("__all__ = [")
    if all_start != -1:
        src = src[:all_start].rstrip("\n")
    all_block = "__all__ = [\n" + "".join(f'    "{n}",\n' for n in sorted(REQUIRED)) + "]\n"
    src = src.rstrip("\n") + "\n\n\n" + all_block

    FACADE.write_text(src)

    subprocess.run([".venv/bin/ruff", "check", str(FACADE), "--fix", "--quiet"], check=False)


if __name__ == "__main__":
    main()
