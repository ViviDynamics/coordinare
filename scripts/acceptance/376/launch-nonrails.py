"""Native Task 8 adaptation of the reviewed 353 bootstrap launcher.

Only checkout/artifact locations and obsolete legacy backend patch differ; execution gates and budgets are retained.

Fresh logical attempt per run (standing authority), production pipeline only: discovery, independent source and
executable review, probes, approvals, installation, offline clean consumer and cleanup, through
scripts/validate_environment_repositories.main with unchanged gates. Budgets: 8 scope, 8 executable, 8 schema,
4 execution repairs (raised from 2 by Jason's ruling, 2026-10-31). Runtime: 8 CPUs (the guest gets half), 4096 MiB, 7200 s sessions, native guest plus the other
CPU's guest as alternate.

usage: 353-launch.py REPO LABEL --base sha256:IMAGE --head SHA [--reuse-scope-from DIR] [--no-execute] [--unattended]
       [--resume-from DIR [--resume-attempt N] [--resume-max-repairs N] [--resume-seconds S]]

--unattended (Jason, 2026-11-01): probes and execution run without operator approval, as in the daemon.

--resume-from (maintainer ruling 2026-11-27) is a DEBUG mode that never counts toward the ship gate: it starts at the
installer from a finished run's accepted contract, rebound to this run's image, with no discovery, review or model
call. Its result.json says gate_eligible false, a pass is status "resume_ready" (never "ready") and the launcher exits
non-zero. It uses a one-hour attempt (one more hour per raised repair) and no execution repairs unless --resume-max-repairs
raises them.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

parser = argparse.ArgumentParser()
parser.add_argument('repo')
parser.add_argument('label', help='fresh profile label, e.g. dettmore-e2e-01')
parser.add_argument('--base', required=True, help='local immutable carrier image id')
parser.add_argument('--head', required=True, help='commit whose full local suite and preflight passed')
parser.add_argument('--reuse-scope-from', type=Path, default=None)
parser.add_argument('--no-execute', action='store_true', help='discovery and review only (Elm scope lane)')
parser.add_argument('--unattended', action='store_true', help='no operator approval gates (daemon path)')
parser.add_argument('--resume-from', type=Path, default=None,
                    help='DEBUG ONLY, never gate evidence: a finished run directory whose accepted contract is executed')
parser.add_argument('--resume-attempt', type=int, default=None, help='the source attempt-00N (default: the last accepted)')
parser.add_argument('--resume-max-repairs', type=int, default=0, help='execution repairs for a resumed run (default 0)')
parser.add_argument('--resume-seconds', type=int, default=None,
                    help='attempt seconds for a resumed run (default one hour, plus one per raised repair; at most 8 h)')
args = parser.parse_args()
RESUME_ATTEMPT_SECONDS = 3600
RESUME_MODEL_CALLS = 512

from dotenv import dotenv_values

from coordinare_service_inference.environment.runtime import RuntimePolicy
from scripts import validate_environment_repositories as validation
from scripts.environment_validation_image import candidate_image

STATE = ROOT/'.agents/state'
PRIOR_STATE = Path.home()/'Workspaces/ViviDynamics/coordinare-353-validation-recovery/.agents/state'
OUTPUT = STATE/'376-org-validation'/args.label
IMAGE_OUTPUT = STATE/'376-org-validation'/(args.label+'-image')
SNAPSHOTS = STATE/'376-snapshots'

# Refusals before any image creation or model charge. Explicit raises, never assert: python -O drops asserts.
def require(condition: object, message: str) -> None:
    if not condition:
        raise SystemExit('launch refused: ' + message)


require(not OUTPUT.exists() and not OUTPUT.is_symlink() and not IMAGE_OUTPUT.exists(),
        'fresh labeled profile required; never renew an attempt')
require((SNAPSHOTS/args.repo).is_dir(), 'pinned repository snapshot missing')
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
require(head == args.head, 'run checkout HEAD differs from the validated head')
require(not subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], text=True).strip(),
        'tracked tree must be clean')
require(subprocess.run(['git', 'diff', '--quiet', head, '--', 'src', 'packages', 'scripts', 'agent']).returncode == 0,
        'product code differs from the validated head')
import coordinare
import coordinare_service_inference
import scripts.validate_environment_repositories as _harness
for _module in (coordinare, coordinare_service_inference, _harness):
    require(Path(_module.__file__).resolve().is_relative_to(ROOT.resolve()),
            f'{_module.__name__} imports from outside this checkout; use this checkout\'s .venv')
proof = json.loads((STATE/f'376-local-validation-{head}.json').read_text())
require(proof.get('head_sha') == head and proof.get('test_all') == 'pass' and proof.get('preflight') == 'pass',
        'exact-head full local suite and preflight must pass (CI may still be running; stop the run if it fails)')
if args.reuse_scope_from is not None:
    require((args.reuse_scope_from/args.repo/'environment-attempt.json').is_file(), 'scope source journal missing')
if args.resume_from is not None:
    require(args.reuse_scope_from is None, '--resume-from cannot be combined with --reuse-scope-from')
    require(not args.no_execute, '--resume-from exists only to execute; it cannot be combined with --no-execute')
    require((args.resume_from/args.repo/'result.json').is_file(), 'resume source has no result.json (unfinished run)')
    require(0 <= args.resume_max_repairs <= 8, '--resume-max-repairs must be between 0 and 8')
    require(args.resume_seconds is None or 600 <= args.resume_seconds <= 28800, '--resume-seconds must be 600 to 28800')
else:
    require(args.resume_attempt is None and args.resume_max_repairs == 0 and args.resume_seconds is None,
            '--resume-attempt, --resume-max-repairs and --resume-seconds require --resume-from')
space = subprocess.run(['docker', 'run', '--rm', '--pull', 'never', '--network', 'none', '--read-only', '--cap-drop',
    'ALL', '--security-opt', 'no-new-privileges', '--entrypoint', 'python', args.base,
    '-c', 'import os; s=os.statvfs("/"); print(s.f_bavail*s.f_frsize)'], capture_output=True, text=True, check=True,
    timeout=45)
# The runtime admits its guest only with 8+16+16 GiB free; nare-native-035 launched with 47 GiB and was refused at its
# installer two hours later, after image builds grew the build cache. Maintainer ruling 2026-11-24: 10 GiB of headroom.
require(int(space.stdout.strip()) >= (8+16+16+10)*1024**3, 'need 50 GiB free: the 40 GiB cache/scratch/reserve policy plus 10 GiB headroom')

# 2026-11-26: the 353 checkout holding 353-haiku-route.json was deleted; its route fields live here now.
route = json.loads((STATE/'376-route.json').read_text())
# The gateway URL lives in the untracked route file, never in a tracked copy (no internal hostnames in the repo).
require(isinstance(route.get('base_url'), str) and route['base_url'].startswith('https://'), 'route base_url missing')
route['model'] = 'spark/glm-5.3-flash'
require(route['validated'] is True, 'route smoke test must have passed')
key = dotenv_values(Path.home()/'Workspaces/ViviDynamics/infrastructure/.env').get('LITELLM_MASTER_KEY')
require(key, 'LiteLLM credential missing')
os.environ[route['api_key_env']] = key
del key

# Maintainer ruling 2026-11-25 (nare-native-039): the installer's cache is a bind mount of the run directory, which on
# macOS is case-insensitive APFS, so a toolchain whose file names differ only in case (uv python install's terminfo)
# failed here though it installs on Linux. Each run directory lives on its own case-sensitive APFS sparse volume,
# linked from the usual place. Reattach after a reboot: hdiutil attach -nobrowse -mountpoint VOLUMES/LABEL IMAGE.
VOLUMES = STATE/'376-volumes'


def case_sensitive_output(label: str) -> Path:
    if sys.platform != 'darwin':
        return OUTPUT
    image, mount = VOLUMES/f'{label}.sparseimage', VOLUMES/label
    require(not image.exists() and not mount.exists(), 'fresh run volume required; never renew an attempt')
    mount.mkdir(parents=True)
    subprocess.run(['hdiutil', 'create', '-size', '300g', '-type', 'SPARSE', '-fs', 'Case-sensitive APFS',
                    '-volname', label[:27], str(image)], check=True, capture_output=True, timeout=300)
    subprocess.run(['hdiutil', 'attach', '-nobrowse', '-mountpoint', str(mount), str(image)], check=True,
                   capture_output=True, timeout=300)
    probe = mount/'case-probe'
    probe.write_text('')
    require(not (mount/'CASE-PROBE').exists(), 'run volume is not case-sensitive')
    probe.unlink()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.symlink_to(mount/label)
    return mount/label


RUN_OUTPUT = case_sensitive_output(args.label)

import faulthandler

_stacks = open(STATE/f'376-run-{args.label}.stacks', 'a')  # noqa: SIM115 - lives for the process
faulthandler.register(signal.SIGUSR1, file=_stacks, all_threads=True)


def interrupted(signum, frame):
    raise KeyboardInterrupt(f'controller interrupted: {signum}')


signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)


@dataclass(frozen=True)
class TwoHourProfile(validation.Profile):
    installer_timeout: float = 7200  # Jason, 2026-10-27: two-hour guest sessions


validation.Profile = TwoHourProfile
original_write = validation._write


def annotated_write(path, value):
    if path.name == 'profile.json':
        value = {**value, 'serving_route': {'endpoint': route['base_url'], 'model': route['model'], 'kind': 'litellm'},
                 'agent_runtime': 'nare', 'launcher': '376-launch-nonrails.py', 'label': args.label,
                 'validated_head': head}
        if args.resume_from is not None:
            value['resumed_from'] = {'source_dir': str(args.resume_from.resolve()), 'attempt': args.resume_attempt,
                                     'max_execution_repairs': args.resume_max_repairs,
                                     'attempt_seconds': args.resume_seconds}
            value['gate_eligible'] = False
    original_write(path, value)


validation._write = annotated_write

os.environ['DOCKER_BUILDKIT'] = '0'  # local immutable image id; BuildKit reads it as a registry tag
with candidate_image(args.base, IMAGE_OUTPUT, allow_local_image_id=True) as image:
    identity = json.loads((IMAGE_OUTPUT/'identity.json').read_text())

    def measure(guest):
        run = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--entrypoint', 'python', image,
            '-m', 'coordinare_service_inference.environment.runtime_bundle', guest],
            capture_output=True, text=True, timeout=45)
        if run.returncode:
            raise RuntimeError('platform identity measurement failed before model execution')
        return {'schema_version': 1, 'carrier_image': image, 'carrier_platform': identity['platform'],
                **json.loads(run.stdout)}

    native = identity['platform']
    foreign = 'linux/x86_64' if native == 'linux/aarch64' else 'linux/aarch64'
    policy = RuntimePolicy.model_validate({'schema_version': 1, 'kind': 'private_service_guest',
        'identity': measure(native), 'alternate_identity': measure(foreign),
        'resources': {'cpus': 8, 'memory_mib': 4096, 'cache_bytes': 8*1024**3,
                      'scratch_bytes': 16*1024**3, 'reserve_bytes': 16*1024**3, 'max_seconds': 7200},
        'control_port': 2222, 'max_endpoints': 16})
    policy_path = IMAGE_OUTPUT/'runtime-policy.json'
    policy_path.write_text(policy.model_dump_json(indent=2))
    sys.argv = [__file__, args.repo, '--snapshots', str(SNAPSHOTS), '--output', str(RUN_OUTPUT), '--image', image,
        '--runtime-policy', str(policy_path), '--model', route['model'], '--base-url', route['base_url'],
        '--api-key-env', route['api_key_env'], '--max-tokens', '32768', '--effort', 'low',
        '--max-review-context-bytes', '1048576', '--phase-timeout', '7200', '--structured-transport', 'json_object',
        '--max-total-reads', '8192', '--context-tokens', '262144',
        '--agent-concurrency', '6', '--max-schema-repairs', '16', '--max-coverage-revisions', '8',
        '--max-scope-revisions', '8', '--approval-timeout', '1800',
        '--save-transcripts',
        *(['--max-model-calls', '2048', '--attempt-timeout', '28800', '--max-execution-repairs', '8']
          if args.resume_from is None else
          ['--max-model-calls', str(RESUME_MODEL_CALLS),
           # Each raised repair needs its own discovery and execution time; the product caps an attempt at 8 h.
           '--attempt-timeout', str(args.resume_seconds if args.resume_seconds is not None
                                    else min(28800, RESUME_ATTEMPT_SECONDS * (1 + args.resume_max_repairs))),
           '--max-execution-repairs', str(args.resume_max_repairs), '--resume-from', str(args.resume_from.resolve()),
           *(['--resume-attempt', str(args.resume_attempt)] if args.resume_attempt is not None else [])]),
        *(['--reuse-scope-from', str(args.reuse_scope_from)] if args.reuse_scope_from is not None else []),
        *([] if args.no_execute else ['--execute']),
        *(['--unattended'] if args.unattended else [])]
    raise SystemExit(validation.main())
