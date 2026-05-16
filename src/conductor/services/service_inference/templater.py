"""Pure-function template renderer: ServicesManifest → (start, stop, health) shell scripts.

The templates live at `agent/performer/services-templates/*.j2`. They are loaded once,
rendered with Jinja2's StrictUndefined, and returned as plain strings. No subprocess,
no I/O beyond the initial template read.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader, StrictUndefined

if TYPE_CHECKING:
    from coordinare.services.service_inference.schema import ServicesManifest

_REPO_ROOT = Path(__file__).resolve().parents[4]
TEMPLATES_DIR = _REPO_ROOT / "agent" / "performer" / "services-templates"


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
