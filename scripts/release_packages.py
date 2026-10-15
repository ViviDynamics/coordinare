from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

PROJECTS = {
    "coordinare": ".",
    "performer": "agent/performer",
    "coordinare-service-inference": "packages/service_inference",
    "coordinare-ci-detection": "packages/ci_detection",
    "coordinare-routing-models": "packages/routing_models",
}
SHARED_PACKAGES = frozenset(name for name in PROJECTS if name.startswith("coordinare-"))
RELEASE_VERSION = re.compile(r"[1-9][0-9]{3}\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))?")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def prepare_projects(root: Path, stage: Path, version: str, repository: str) -> list[Path]:
    """Stage tagged distributions without modifying source metadata or locks."""
    if not RELEASE_VERSION.fullmatch(version):
        raise ValueError(f"Invalid release version: {version!r}")
    if not REPOSITORY.fullmatch(repository):
        raise ValueError(f"Invalid release repository: {repository!r}")
    projects = []
    for name, relative in PROJECTS.items():
        source = root / relative
        destination = stage / name
        destination.mkdir(parents=True)
        shutil.copytree(source / "src", destination / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for filename in ("LICENSE", "NOTICE"):
            shutil.copy2(root / filename, destination / filename)
        if (source / "README.md").is_file():
            shutil.copy2(source / "README.md", destination / "README.md")
        if name == "coordinare":
            for example in root.glob("config.example*.yaml"):
                shutil.copy2(example, destination / example.name)
        metadata = (source / "pyproject.toml").read_text(encoding="utf-8")
        # These editable paths are checkout-only. Released sdists must resolve
        # their tagged dependencies even when no workspace directories exist.
        metadata = re.sub(r"^\[tool\.uv\.sources\]\n.*?(?=^\[|\Z)", "", metadata, flags=re.MULTILINE | re.DOTALL)
        if name == "coordinare":
            metadata = metadata.replace('dynamic = ["version"]', f'version = "{version}"', 1)
            (destination / "src/coordinare/_version.py").write_text(
                f'from __future__ import annotations\n\n__version__ = "{version}"\n',
                encoding="utf-8",
            )
        metadata, replacements = re.subn(r'^version = "[^"]+"$', f'version = "{version}"', metadata, count=1, flags=re.MULTILINE)
        if replacements != 1:
            raise ValueError(f"Missing static version in {source}")
        for dependency in SHARED_PACKAGES:
            wheel = f"{dependency.replace('-', '_')}-{version}-py3-none-any.whl"
            url = f"https://github.com/{repository}/releases/download/{version}/{wheel}"
            metadata = metadata.replace(f'"{dependency}",', f'"{dependency} @ {url}",')
        if name != "coordinare":
            metadata = metadata.replace('[project]\n', '[project]\nlicense-files = ["LICENSE", "NOTICE"]\n', 1)
        (destination / "pyproject.toml").write_text(metadata, encoding="utf-8")
        projects.append(destination)
    return projects


def copy_installer(root: Path, output: Path) -> None:
    installer = root / "install.sh"
    if installer.is_file():
        shutil.copy2(installer, output / installer.name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build all Python release assets from the release tag")
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to build release packages")
    with tempfile.TemporaryDirectory(prefix="coordinare-release-") as temporary:
        for project in prepare_projects(root, Path(temporary), args.version, args.repository):
            subprocess.run([uv, "build", "--no-sources", "--out-dir", str(output), str(project)], check=True)
    copy_installer(root, output)
    # The smoke install overrides only unpublished sibling URLs, using these
    # exact built wheels. This file is outside dist and is never a release asset.
    overrides = []
    for wheel in sorted(output.glob("*.whl")):
        name = wheel.name.split("-", 1)[0].replace("_", "-")
        if name in SHARED_PACKAGES:
            overrides.append(f"{name} @ {wheel.as_uri()}")
    output.with_suffix(".overrides.txt").write_text("\n".join(overrides) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
