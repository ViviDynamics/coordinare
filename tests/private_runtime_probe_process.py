"""Process collection for the opt-in private-runtime integration harness."""
from __future__ import annotations

import subprocess


def collect_probe(process: subprocess.Popen[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Reap the controller on any interruption, allowing its owned cleanup to run."""
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
