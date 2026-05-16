"""Manual-override path for service inference (Phase 1 of spec 063).

If a project ships `.coordinare/score.json`, the LLM agent is skipped entirely:
we read the file, validate it against `ServicesManifest`, render the three shell
scripts, optionally dry-run them, and drop the artifacts into the env-cache
output directory.

This module is pure logic + filesystem I/O — no subprocesses unless
`run_validation=True`. It is safe to call from either the performer or a unit
test.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from coordinare.services.service_inference.schema import ServicesManifest
from coordinare.services.service_inference.templater import RenderedScripts, render
from coordinare.services.service_inference.validator import ValidationResult, validate

OVERRIDE_RELATIVE_PATH = Path(".coordinare") / "score.json"
OVERRIDE_PATH_ENV_VAR = "COORDINARE_MANUAL_OVERRIDE_PATH"
SERVICES_SUBDIR = "services"


def _resolve_override_path(project_root: Path, override_path: Path | None) -> Path:
    """Pick the override file path.

    Precedence: explicit ``override_path`` arg > ``COORDINARE_MANUAL_OVERRIDE_PATH``
    env var > the default ``.coordinare/score.json``. Relative paths are
    resolved against ``project_root``.
    """
    candidate: Path
    if override_path is not None:
        candidate = override_path
    elif (env_val := os.environ.get(OVERRIDE_PATH_ENV_VAR)):
        candidate = Path(env_val)
    else:
        candidate = OVERRIDE_RELATIVE_PATH
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate


@dataclass(frozen=True)
class ManualOverrideResult:
    applied: bool
    reason: str
    manifest: ServicesManifest | None = None
    scripts_dir: Path | None = None
    validation: ValidationResult | None = None


def apply_manual_override(
    project_root: Path,
    output_root: Path,
    *,
    run_validation: bool = False,
    override_path: Path | None = None,
) -> ManualOverrideResult:
    """Look for the manual override file, validate, render, and drop artifacts.

    `project_root` is the checked-out source tree.
    `output_root` is the env-cache directory (e.g. `/devenv/<sym>`); we write into
    `<output_root>/services/`.
    `override_path` lets callers point at a non-default location; otherwise
    the ``COORDINARE_MANUAL_OVERRIDE_PATH`` env var, then ``.coordinare/score.json``,
    is consulted.

    Returns `applied=False` with a reason when no override file is present —
    callers fall through to the LLM agent in Phase 2 (Phase 1 just builds an
    empty manifest).
    """
    override_path = _resolve_override_path(project_root, override_path)
    if not override_path.is_file():
        return ManualOverrideResult(
            applied=False,
            reason=f"no override file at {override_path}",
        )

    try:
        raw = json.loads(override_path.read_text())
    except json.JSONDecodeError as exc:
        return ManualOverrideResult(
            applied=False,
            reason=f"{override_path} is not valid JSON: {exc}",
        )

    # Force-set rather than setdefault: a hand-authored override must always be
    # tagged "manual-override" so downstream telemetry (and the cache-key
    # composition in cache_key.py) can distinguish it from LLM-produced
    # manifests. An operator who typed a different agent_version is corrected.
    raw["agent_version"] = "manual-override"

    try:
        manifest = ServicesManifest.model_validate(raw)
    except ValidationError as exc:
        return ManualOverrideResult(
            applied=False,
            reason=f"{override_path} does not match ServicesManifest schema:\n{exc}",
        )

    scripts = render(manifest)

    validation: ValidationResult | None = None
    if run_validation:
        validation = validate(scripts)
        if not validation.ok:
            return ManualOverrideResult(
                applied=False,
                reason=f"manual override scripts failed dry-run: {validation.summary}",
                manifest=manifest,
                validation=validation,
            )

    scripts_dir = output_root / SERVICES_SUBDIR
    _write_artifacts(scripts_dir, manifest, scripts)

    return ManualOverrideResult(
        applied=True,
        reason="manual override applied",
        manifest=manifest,
        scripts_dir=scripts_dir,
        validation=validation,
    )


def _write_artifacts(
    scripts_dir: Path, manifest: ServicesManifest, scripts: RenderedScripts
) -> None:
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "services.json").write_text(
        manifest.model_dump_json(indent=2) + "\n"
    )
    for name, body in (
        ("services-start.sh", scripts.start),
        ("services-stop.sh", scripts.stop),
        ("services-health.sh", scripts.health),
    ):
        path = scripts_dir / name
        path.write_text(body)
        path.chmod(0o755)
    sidecar = "\n".join(manifest.cache_inputs) + ("\n" if manifest.cache_inputs else "")
    (scripts_dir / "cache_manifest.txt").write_text(sidecar)
