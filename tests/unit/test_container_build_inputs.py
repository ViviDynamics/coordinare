"""The two reasons `docker compose up --build` could not build (PR #219).

Neither is exotic and both were invisible to every existing test, because nothing
here reads the Dockerfile or the ignore file. They were found by running the build
rather than by reading it, and both are the kind of thing that silently comes back.

1. `.dockerignore` excluded `.env*`, which also excluded `.env.example` -- and the
   root Dockerfile COPYs that file. The build died on it before reaching anything
   else, with "/.env.example: not found".
2. `pyproject` depends on `coordinare-service-inference`, which resolves through
   `[tool.uv.sources]` to a path in this repo. `pip` does not read that table, and
   the name 404s on PyPI, so `pip install .` could not resolve it.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path

import pytest

DOCKERIGNORE = Path(".dockerignore")
ROOT_DOCKERFILE = Path("Dockerfile")
DAEMON_DOCKERFILE = Path("Dockerfile.daemon")
PYPROJECT = Path("pyproject.toml")


def _ignore_lines() -> list[str]:
    return [
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


class TestTheBuildContextCarriesWhatTheDockerfileCopies:
    def test_every_copied_path_survives_the_ignore_file(self) -> None:
        """A COPY of a path the ignore file excludes is a build that cannot run."""
        ignored = set(_ignore_lines())
        negated = {line[1:] for line in ignored if line.startswith("!")}
        offenders = []
        for dockerfile in (ROOT_DOCKERFILE, DAEMON_DOCKERFILE):
            for match in re.finditer(r"^COPY\s+(?!--)(.+)$", dockerfile.read_text(), re.MULTILINE):
                sources = match.group(1).split()[:-1]
                for source in sources:
                    # removeprefix, not lstrip: lstrip takes a character set, so
                    # ".env.example".lstrip("./") is "env.example" -- which then
                    # matched no pattern and made this test blind to the exact
                    # defect it was written for. Found by mutation, not by review.
                    name = source.removeprefix("./")
                    # Only the literal-file cases; a directory is not matched by
                    # these glob rules and the ones here are all plain names.
                    if "*" in name or "/" in name:
                        continue
                    # fnmatch rather than a hand-rolled endswith("*"): review
                    # noted the original understood one wildcard and would have
                    # been wrong about `?`, a character class, or a pattern with
                    # the star anywhere but the end. Docker's matcher is Go's
                    # filepath.Match, which fnmatch is close enough to for the
                    # plain names this checks.
                    hidden = any(
                        fnmatch(name, pattern)
                        for pattern in ignored
                        if not pattern.startswith("!")
                    )
                    if hidden and name not in negated:
                        offenders.append(f"{dockerfile.name}: COPY {source}")

        assert not offenders, (
            f"these COPY sources are excluded by .dockerignore, so the build fails "
            f"on them: {offenders}"
        )

    def test_the_env_example_is_reachable(self) -> None:
        assert "!.env.example" in _ignore_lines()

    def test_the_real_env_is_still_excluded(self) -> None:
        """The negation names one file. A `!.env*` here would ship the secrets."""
        negations = [line for line in _ignore_lines() if line.startswith("!.env")]

        assert negations == ["!.env.example"], (
            f"unexpected .env negation in .dockerignore: {negations}. Anything "
            "broader than the example file puts real credentials in the image."
        )


class TestThePathDependencyIsInstallable:
    """`pip install .` cannot resolve a `[tool.uv.sources]` path dependency."""

    @pytest.mark.parametrize("dockerfile", (ROOT_DOCKERFILE, DAEMON_DOCKERFILE))
    def test_an_image_that_installs_the_project_copies_the_package(self, dockerfile) -> None:
        text = dockerfile.read_text()
        if "coordinare-service-inference" not in PYPROJECT.read_text():
            pytest.skip("the path dependency is gone; nothing to copy")
        installs = "pip install" in text or "uv sync" in text
        if not installs:
            pytest.skip(f"{dockerfile.name} does not install the project")

        assert "COPY packages" in text, (
            f"{dockerfile.name} installs the project without copying packages/. "
            "coordinare-service-inference resolves to a path in this repo and 404s on PyPI."
        )

    def test_the_pip_image_installs_the_local_package_explicitly(self) -> None:
        """uv reads [tool.uv.sources]; pip does not, so pip needs it named."""
        text = ROOT_DOCKERFILE.read_text()
        if "pip install" not in text:
            pytest.skip("the root image no longer installs with pip")

        assert "packages/service_inference" in text, (
            "pip ignores [tool.uv.sources], so `pip install .` alone cannot resolve "
            "coordinare-service-inference. Install the local path first."
        )
