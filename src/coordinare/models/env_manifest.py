"""Env-bootstrap dependency manifest (077).

Coordinare derives an explicit, authoritative checklist of dependencies from a
symphony's README + structured project files (`.ruby-version`, `Gemfile`,
`package.json`, …).  The env_bootstrap agent installs *against* this manifest
and coordinare *verifies* against the same manifest — so a spec-pinned tool
version (e.g. Ruby 3.4.2) can never be silently missed by a free-form agent.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ItemKind = Literal["runtime", "gem", "system", "node_pkg"]
"""
- ``runtime``  — a pinned language/runtime (ruby, node, python) installed via a
  version manager / build tool into the cache prefix.
- ``gem``      — a Ruby gem that must be installed and loadable.
- ``system``   — an OS package / binary that must be on PATH (e.g. chromium).
- ``node_pkg`` — a global/declared Node package.
"""


class ManifestItem(BaseModel):
    """One required dependency, with how to verify it."""

    name: str = Field(description="Canonical dependency name, e.g. 'ruby', 'rails', 'chromium'.")
    kind: ItemKind = Field(description="What category of dependency this is.")
    version: str | None = Field(
        default=None,
        description="Exact pinned version when known (e.g. '3.4.2'); None if unpinned.",
    )
    source: str = Field(
        description="Where this item was derived from, e.g. '.ruby-version', 'Gemfile', 'README.md'."
    )
    check: str | None = Field(
        default=None,
        description=(
            "Shell snippet that exits non-zero if the dependency is missing/wrong. "
            "When None, the verify renderer generates a sensible default for the kind."
        ),
    )
    install_hint: str | None = Field(
        default=None,
        description="Optional hint for HOW to install (e.g. 'rbenv install 3.4.2'). Advisory.",
    )


class EnvManifest(BaseModel):
    """The full dependency checklist for one symphony's env cache."""

    symphony_name: str
    items: list[ManifestItem] = Field(default_factory=list)
    spec_sha: str | None = Field(
        default=None,
        description="The combined spec SHA this manifest was derived at (for cache keying).",
    )
    llm_derived: bool = Field(
        default=False,
        description="True if an LLM README pass contributed items (vs. deterministic-only).",
    )

    def runtime_pins(self) -> list[ManifestItem]:
        """Pinned language runtimes — install these FIRST (gems/modules build on them)."""
        return [i for i in self.items if i.kind == "runtime" and i.version]
