"""Issue #415: bandit scans only Python targets, so a doc change cannot block the floor."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from performer.models import Score, Stand
from performer.security_floor import run_security_floor
from performer.security_scanner import ScanDiagnostic, ScannerSetupError, scan_diff

PARSE_ERROR = "syntax error while parsing AST from file"


def _bandit_result(filename: str) -> dict:
    return {"filename": filename, "line_number": 2, "issue_severity": "HIGH",
            "test_id": "B605", "test_name": "start_process_with_a_shell"}


class FakeScanners:
    """Models the real tools: bandit cannot parse a file that is not Python."""

    def __init__(self, root: Path):
        self.root = root
        self.calls: dict[str, list[str]] = {}

    def __call__(self, cmd, **kwargs):
        tool = Path(cmd[0]).name
        targets = [arg for arg in cmd if arg.startswith("target_") or arg.startswith("./")]
        self.calls[tool] = targets
        if tool == "semgrep":
            return SimpleNamespace(stdout='{"results": [], "errors": []}', returncode=0)
        errors, results = [], []
        for name in targets:
            source = (self.root / name).read_bytes()
            if not _parses_as_python(source):
                errors.append({"filename": name, "reason": PARSE_ERROR})
            elif b"os.system" in source:
                results.append(_bandit_result(name))
        return SimpleNamespace(stdout=json.dumps({"results": results, "errors": errors}),
                               returncode=1 if results else 0)


def _parses_as_python(source: bytes) -> bool:
    try:
        compile(source, "<target>", "exec")
    except SyntaxError:
        return False
    return True


def _scan(root: Path, files: dict[str, str | bytes]):
    for name, content in files.items():
        if isinstance(content, bytes):
            (root / name).write_bytes(content)
        else:
            (root / name).write_text(content)
    fake = FakeScanners(root)
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


@pytest.mark.parametrize("head", [
    "#!/usr/bin/env python3\n",
    "#!/usr/bin/python\n",
    "\ufeff#!/usr/bin/env python3\n",
    "#!/usr/bin/env -S uv run --script\n",
    "",
])
def test_extensionless_python_still_goes_to_bandit(tmp_path, head):
    findings, calls = _scan(tmp_path, {"target_0.scanned": f"{head}{VULNERABLE}",
                                       "target_1.scanned": "#!/bin/sh\necho hi | wc -l\n"})
    assert calls["bandit"] == ["target_0.scanned"]
    assert [f["file"] for f in findings] == ["target_0.scanned"]


@pytest.mark.parametrize("shebang", ["#!/usr/bin/env python3", "#!/usr/bin/env python3.14t",
                                     "#!/usr/bin/python3-dbg", "#!/usr/bin/env pythonw"])
def test_unparsable_file_with_a_python_shebang_still_fails(tmp_path, shebang):
    with pytest.raises(ScanDiagnostic, match="bandit reported scan errors"):
        _scan(tmp_path, {"target_0.scanned": f"{shebang}\nprint 'py2'\n"})


def test_configs_that_happen_to_parse_as_python_are_not_bandit_targets(tmp_path):
    _, calls = _scan(tmp_path, {"target_0.ini": "[app]\ndebug = True\n", "target_1.json": '{"a": 1}\n',
                                "target_2.txt": "main\n", "target_3.ipynb": '{"cells": []}\n'})
    assert "bandit" not in calls


def test_extensionless_binary_is_not_a_bandit_target(tmp_path):
    _, calls = _scan(tmp_path, {"target_0.scanned": b"\x7fELF\x02\x01\x01\x00" + b"x = 1\n" * 10})
    assert "bandit" not in calls


def test_extensionless_file_too_large_to_classify_goes_to_bandit(tmp_path):
    with patch("performer.security_scanner._CONTENT_CHECK_MAX_BYTES", 64):
        with pytest.raises(ScanDiagnostic, match="bandit reported scan errors"):
            _scan(tmp_path, {"target_0.scanned": "Plain text that is not code.\n" * 10})


def test_floor_export_suffix_is_the_one_the_scanner_treats_as_extensionless():
    from performer import security_floor, security_scanner

    assert security_floor._EXPORT_DEFAULT_SUFFIX == security_scanner.EXTENSIONLESS_SUFFIX


def test_other_interpreters_named_like_python_are_not_bandit_targets(tmp_path):
    _, calls = _scan(tmp_path, {"target_0.scanned": "#!/usr/bin/env ipython\n%matplotlib inline\n"})
    assert "bandit" not in calls


def test_missing_bandit_still_fails_a_diff_without_python(tmp_path):
    (tmp_path / "target_0.md").write_text(DOC)
    fake = FakeScanners(tmp_path)

    def resolve(tool):
        if tool == "bandit":
            raise ScannerSetupError("missing", tool="bandit")
        return f"/usr/bin/{tool}"

    with patch("performer.security_scanner._resolve_binary", side_effect=resolve), \
            patch("performer.security_scanner.subprocess.run", side_effect=fake), \
            pytest.raises(ScannerSetupError):
        scan_diff(["target_0.md"], tmp_path)


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
