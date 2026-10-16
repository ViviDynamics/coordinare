from __future__ import annotations

import fnmatch
import re
import shlex
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FORCE_INCLUDED = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]


@pytest.mark.parametrize("source", FORCE_INCLUDED)
def test_force_included_path_is_whitelisted_by_dockerignore(source: str) -> None:
    included = False
    for line in (ROOT / ".dockerignore").read_text().splitlines():
        pattern = line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        if fnmatch.fnmatchcase(source, pattern.removeprefix("!").lstrip("/")):
            included = pattern.startswith("!")
    assert included, f"{source} needs a .dockerignore whitelist entry"


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "Dockerfile.daemon"])
@pytest.mark.parametrize("source", FORCE_INCLUDED)
def test_force_included_path_is_copied_before_docker_install(source: str, dockerfile: str) -> None:
    text = (ROOT / dockerfile).read_text().replace("\\\n", " ")
    install = re.search(r"^RUN\s+(?:pip install|uv sync)\b", text, re.MULTILINE)
    assert install is not None, f"No project install step found in {dockerfile}"
    copied = []
    for line in text[:install.start()].splitlines():
        if line.startswith("COPY "):
            copied.extend(shlex.split(line)[1:-1])
    assert any(fnmatch.fnmatchcase(source, pattern) for pattern in copied), (
        f"{source} must be copied before installation in {dockerfile}"
    )
