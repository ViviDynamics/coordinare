from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinare.__main__ import _build_arg_parser
from coordinare.init_answers import ANSWER_NAMES

ROOT = Path(__file__).resolve().parents[2]
GUIDES = (
    "README.md",
    "CONTRIBUTING.md",
    "docs/operators/containerized-performers.md",
    "docs/operators/service-inference-rails-quickstart.md",
    "docs/quickstart.md",
    "docs/quickstart-selfhosted.md",
    "docs/init.md",
    "docs/first-launch.md",
    "docs/releases.md",
    "docs/service.md",
    "docs/docker-mode-setup.md",
    "docs/upgrading.md",
    "docs/uninstalling.md",
    "docs/unattended-install.md",
    "docs/files-and-paths.md",
    "docs/setup-troubleshooting.md",
)
INSTALL_URL = "https://github.com/ViviDynamics/coordinare/releases/latest/download/install.sh"


def shell_commands(document: str) -> list[list[str]]:
    commands = []
    for block in re.findall(r"```(?:sh|bash)\n(.*?)```", document, re.DOTALL):
        for line in block.replace("\\\n", " ").splitlines():
            words = shlex.split(line, comments=True)
            if words:
                commands.append(words)
    return commands


@pytest.mark.parametrize("guide", GUIDES)
def test_documented_cli_commands_parse_with_real_argparse(guide: str) -> None:
    parser = _build_arg_parser()
    for words in shell_commands((ROOT / guide).read_text()):
        if words[0] == "coordinare":
            parser.parse_args(words[1:])
        elif words[1:3] == ["-m", "coordinare"]:
            parser.parse_args(words[3:])


def test_readme_quick_start_is_complete_without_a_checkout() -> None:
    readme = (ROOT / "README.md").read_text()
    quick_start = readme.split("## Quick Start\n", 1)[1].split("\n## ", 1)[0]
    commands = shell_commands(quick_start)
    assert ["coordinare", "init"] in commands
    assert ["coordinare", "doctor"] in commands
    assert ["coordinare"] in commands
    assert INSTALL_URL in quick_start
    assert "http://127.0.0.1:8090" in quick_start
    assert "less install.sh" in quick_start
    assert "git clone" not in readme
    assert "uv sync" not in quick_start


@pytest.mark.parametrize("guide", GUIDES)
def test_installer_examples_accept_their_documented_flags(guide: str) -> None:
    for words in shell_commands((ROOT / guide).read_text()):
        if words[:2] == ["sh", "install.sh"]:
            arguments = words[2:]
        elif words[:3] == ["curl", "-fsSL", INSTALL_URL] and "|" in words:
            shell = words[words.index("|") + 1:]
            assert shell == ["sh"] or shell[:3] == ["sh", "-s", "--"]
            arguments = shell[3:]
        else:
            continue
        result = subprocess.run(
            ["sh", str(ROOT / "install.sh"), *arguments, "--help"],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("guide", GUIDES)
def test_guide_local_links_resolve(guide: str) -> None:
    path = ROOT / guide
    for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
        if "://" not in target and not target.startswith("#"):
            assert (path.parent / target.split("#", 1)[0]).exists(), (guide, target)


def test_unattended_answers_use_supported_keys() -> None:
    text = (ROOT / "docs/unattended-install.md").read_text()
    blocks = re.findall(r"```yaml\n(.*?)```", text, re.DOTALL)
    assert blocks, "Keep a copyable answers file in the unattended guide"
    for block in blocks:
        answers = yaml.safe_load(block)
        assert set(answers) <= set(ANSWER_NAMES)
        assert {"repository", "project_number", "harness", "run_mode"} <= set(answers)
