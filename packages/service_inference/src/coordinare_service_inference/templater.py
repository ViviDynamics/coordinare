"""Pure-function template renderer: ServicesManifest → (start, stop, health) shell scripts.

The templates ship with this package at ``coordinare_service_inference/templates/*.j2``
so they are available whether the code runs from a dev checkout or an installed
wheel (the performer container installs the package and would not have access to
the source-tree-relative ``agent/performer/services-templates`` directory).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader, StrictUndefined

if TYPE_CHECKING:
    from coordinare_service_inference.schema import ServicesManifest

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


@dataclass(frozen=True)
class RenderedScripts:
    start: str
    stop: str
    health: str


def _env(templates_dir: Path | None = None) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir or TEMPLATES_DIR)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
    )
    # `shq` shell-quotes any value interpolated into a generated bash script.
    # Required because manifest fields (binary, data_dir, start_args entries)
    # flow from LLM/operator input into executable shell — unquoted, a
    # malicious or mistyped value containing spaces, $, `, ;, or quotes would
    # inject code. start_args is an argv list, not a shell string, so each
    # element is independently quoted by the template.
    env.filters["shq"] = lambda v: shlex.quote(str(v))
    return env


def render(manifest: ServicesManifest, templates_dir: Path | None = None) -> RenderedScripts:
    """Render the three shell scripts from the given manifest.

    `templates_dir` is exposed for tests; production callers use the default.
    """
    env = _env(templates_dir)
    ctx = manifest.model_dump()
    start = env.get_template("services-start.sh.j2").render(**ctx)
    stop = env.get_template("services-stop.sh.j2").render(**ctx)
    health = env.get_template("services-health.sh.j2").render(**ctx)
    return RenderedScripts(start=start, stop=stop, health=health)
