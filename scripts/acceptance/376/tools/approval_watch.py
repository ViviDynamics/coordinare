"""Block until a run needs an approval or its launcher exits.

usage: approval_watch.py RUN_DIR LABEL
Prints a JSON list of [kind, path] pending approvals (probe proposals without probe-approved.json, accepted
manifests without execution-approved.json), or "launcher exited" when no launcher process for LABEL remains.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

run_dir, label = Path(sys.argv[1]), sys.argv[2]


def pending() -> list[list[str]]:
    found = []
    for proposal in sorted(run_dir.glob('*/probe-*/probe-proposal.json')):
        if not (proposal.parent / 'probe-approved.json').exists():
            found.append(['probe', str(proposal)])
    for manifest in sorted(run_dir.glob('*/attempt-*/accepted-manifest.json')):
        if not (manifest.parent / 'execution-approved.json').exists():
            found.append(['execution', str(manifest)])
    return found


def launcher_alive() -> bool:
    return subprocess.run(['pgrep', '-f', f'376-launch-nonrails.py [a-z]+ {label} --base'],
                          capture_output=True).returncode == 0


while True:
    if items := pending():
        print(json.dumps(items))
        break
    if not launcher_alive():
        print('launcher exited')
        break
    time.sleep(15)
