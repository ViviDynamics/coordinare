"""An interrupted integration wrapper lets its controller finalize owned resources."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def test_timeout_terminates_controller_gracefully_before_forcing_it(tmp_path: Path) -> None:
    from tests.private_runtime_probe_process import collect_probe

    marker = tmp_path / "finalized"
    program = (
        "import signal,sys,time,pathlib\n"
        "def stop(*args):\n"
        " pathlib.Path(sys.argv[1]).write_text('owned cleanup executed')\n"
        " raise SystemExit(1)\n"
        "signal.signal(signal.SIGTERM,stop)\n"
        "print('ready',flush=True)\n"
        "time.sleep(60)\n"
    )
    with subprocess.Popen([sys.executable, "-c", program, str(marker)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        assert process.stdout is not None
        assert process.stdout.readline() == "ready\n"
        with pytest.raises(subprocess.TimeoutExpired):
            collect_probe(process, timeout=0.05)
        assert process.poll() is not None
        assert marker.read_text() == "owned cleanup executed"
