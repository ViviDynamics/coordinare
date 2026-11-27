"""Issue #415: bandit scans only Python targets, so a doc change cannot block the floor."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from performer.models import Score, Stand
from performer.security_floor import run_security_floor
from performer.security_scanner import ScanDiagnostic, scan_diff

PARSE_ERROR = "syntax error while parsing AST from file"


def _bandit_result(filename: str) -> dict:
    return {"filename": filename, "line_number": 2, "issue_severity": "HIGH",
            "test_id": "B605", "test_name": "start_process_with_a_shell"}


class FakeScanners:
    """Models the real tools: bandit cannot parse a file that is not Python."""

    def __init__(self, root: Path, *, unparsable: set[str] = frozenset()):
        self.root = root
        self.unparsable = set(unparsable)
        self.calls: dict[str, list[str]] = {}

    def __call__(self, cmd, **kwargs):
        tool = Path(cmd[0]).name
        targets = [arg for arg in cmd if arg.startswith("target_") or arg.startswith("./")]
        self.calls[tool] = targets
        if tool == "semgrep":
            return SimpleNamespace(stdout='{"results": [], "errors": []}', returncode=0)
        errors, results = [], []
        for name in targets:
            text = (self.root / name).read_text()
            if name in self.unparsable or not _parses_as_python(text):
                errors.append({"filename": name, "reason": PARSE_ERROR})
            elif "os.system" in text:
                results.append(_bandit_result(name))
        return SimpleNamespace(stdout=json.dumps({"results": results, "errors": errors}),
                               returncode=1 if results else 0)


def _parses_as_python(text: str) -> bool:
    try:
        compile(text, "<target>", "exec")
    except SyntaxError:
        return False
    return True


def _scan(root: Path, files: dict[str, str], **kwargs):
    for name, text in files.items():
        (root / name).write_text(text)
    fake = FakeScanners(root, **kwargs)
    with patch("performer.security_scanner._resolve_binary", side_effect=lambda tool: f"/usr/bin/{tool}"), \
            patch("performer.security_scanner.subprocess.run", side_effect=fake):
        findings = scan_diff(list(files), root)
    return findings, fake.calls


VULNERABLE = "import os\nos.system(input())\n"
DOC = "# Report\n\nSee the `scan` notes.\n"


def test_markdown_beside_vulnerable_python_reports_bandit_findings(tmp_path):
    findings, calls = _scan(tmp_path, {"target_0.md": DOC, "target_1.py": VULNERABLE})
    assert calls["semgrep"] == ["target_0.md", "target_1.py"]
    assert calls["bandit"] == ["target_1.py"]
    assert [f["file"] for f in findings] == ["target_1.py"]
    assert findings[0]["description"] == "bandit:B605"


def test_diff_without_python_files_does_not_run_bandit(tmp_path):
    findings, calls = _scan(tmp_path, {"target_0.md": DOC, "target_1.yaml": "a: [\n",
                                       "target_2.scanned": "all:\n\tmake build\n"})
    assert findings == []
    assert calls["semgrep"] == ["target_0.md", "target_1.yaml", "target_2.scanned"]
    assert "bandit" not in calls


def test_python_file_bandit_cannot_parse_still_fails(tmp_path):
    with pytest.raises(ScanDiagnostic, match="bandit reported scan errors"):
        _scan(tmp_path, {"target_0.md": DOC, "target_1.py": "def broken(:\n"})


@pytest.mark.parametrize("name", ["target_0.pyi", "target_0.pyw", "target_0.PY"])
def test_other_python_suffixes_go_to_bandit(tmp_path, name):
    _, calls = _scan(tmp_path, {name: "x: int\n"})
    assert calls["bandit"] == [name]


@pytest.mark.parametrize("shebang", ["#!/usr/bin/env python3", "#!/usr/bin/python"])
def test_extensionless_python_script_still_goes_to_bandit(tmp_path, shebang):
    findings, calls = _scan(tmp_path, {"target_0.scanned": f"{shebang}\n{VULNERABLE}",
                                       "target_1.scanned": "#!/bin/sh\necho hi\n"})
    assert calls["bandit"] == ["target_0.scanned"]
    assert [f["file"] for f in findings] == ["target_0.scanned"]


def test_dash_prefixed_python_target_stays_an_operand(tmp_path):
    _, calls = _scan(tmp_path, {"-config.py": "x = 1\n", "-notes.md": DOC})
    assert calls["bandit"] == ["./-config.py"]
    assert calls["semgrep"] == ["./-config.py", "./-notes.md"]


@pytest.mark.asyncio
async def test_floor_reports_findings_for_a_diff_with_a_doc_change(tmp_path):
    files = {"src/bench/lookup.py": VULNERABLE, "docs/cards/security.md": DOC}
    request = {"version": 1, "diff_status": "ok", "files": list(files), "head_sha": "head"}
    score = Score(title="Card", repo_url="https://github.com/example/project", branch="fix",
                  security_floor=request)

    def git(cmd, **kwargs):
        if "show" in cmd:
            kwargs["stdout"].write(files[cmd[-1].split(":", 1)[1]].encode())
        return SimpleNamespace(stdout=b"" if "ls-tree" in cmd else b"head")

    real_scan_diff = scan_diff

    def scan(names, root):
        fake = FakeScanners(root)
        with patch("performer.security_scanner._resolve_binary", side_effect=lambda tool: f"/usr/bin/{tool}"), \
                patch("performer.security_scanner.subprocess.run", side_effect=fake):
            return real_scan_diff(names, root)

    with patch("performer.security_floor.subprocess.run", side_effect=git), patch(
        "performer.security_floor._head_targets", return_value=list(files),
    ), patch("performer.security_floor.scan_diff", side_effect=scan):
        report = await run_security_floor(score, Stand(path=tmp_path, branch="fix"))
    assert report["complete"] is True, report.get("reason")
    assert [f["file"] for f in report["findings"]] == ["src/bench/lookup.py"]
