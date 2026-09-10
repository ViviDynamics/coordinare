"""339: performer source must never import the `coordinare` package.

The performer image (`agent/performer/Dockerfile.base`) installs
`packages/*` and `agent/performer` only -- it has never shipped `coordinare`.
So any `from coordinare... import ...` in performer source raises ImportError
for every in-container run.

That is not theoretical. `workflows/implementer/baseline.py` imported
`coordinare.services.ci_detection`, took its `except ImportError` branch on
every single container run, and hard-blocked cards with "no test runner
detected: coordinare ci_detection not available" -- reporting a packaging bug
as a repository problem an operator was told no code change could fix. Two
live cards sat blocked on it.

Shared logic belongs in `packages/` (see `coordinare_ci_detection`,
`coordinare_service_inference`), which both sides install.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "performer"


def _coordinare_imports(path: Path) -> list[str]:
    """Every `import coordinare...` / `from coordinare... import` in one file.

    Uses the AST rather than a text scan so that comments and docstrings
    mentioning coordinare -- of which there are many, legitimately -- do not
    register, and so a lazy import inside a function body still does.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "coordinare" or alias.name.startswith("coordinare."):
                    hits.append(f"{path.name}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "coordinare" or mod.startswith("coordinare."):
                hits.append(f"{path.name}:{node.lineno} from {mod} import ...")
    return hits


def test_performer_source_never_imports_coordinare() -> None:
    offenders: list[str] = []
    scanned = 0
    for path in sorted(SRC.rglob("*.py")):
        scanned += 1
        offenders.extend(_coordinare_imports(path))
    assert scanned > 50, f"scan looks broken -- only {scanned} files found under {SRC}"
    assert not offenders, (
        "performer source imports coordinare, which is absent from the performer "
        "image; move the shared logic into packages/ instead:\n  "
        + "\n  ".join(offenders)
    )


def test_ci_detection_is_importable_without_coordinare() -> None:
    """The replacement path must not smuggle the dependency back in."""
    import coordinare_ci_detection

    assert hasattr(coordinare_ci_detection, "detect")
    source = Path(coordinare_ci_detection.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("coordinare"), (
                "the shared package must not depend on coordinare"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("coordinare"), (
                    "the shared package must not depend on coordinare"
                )
