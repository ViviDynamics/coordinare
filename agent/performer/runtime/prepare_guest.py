"""Build a generic platform-only service guest; never reads a consumer repository."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path


PLATFORM_SCRIPT = '''#!/bin/sh
exec >/dev/ttyS0 2>&1
set -eu
trap 'status=$?; if [ $status -ne 0 ]; then echo COORDINARE_PLATFORM_FAILED:$status; poweroff -f; fi' EXIT
mount -t tmpfs -o mode=1777,size=512m tmpfs /tmp
apk add --no-cache docker docker-cli-compose openssh-server python3 e2fsprogs qemu-system-x86_64 seabios
rc-update del sshd default
rc-service cgroups start
rc-service docker start
docker info >/dev/null
test -z "$(docker ps -aq)"
test -z "$(docker volume ls -q)"
rc-service docker stop
rm -f /etc/ssh/ssh_host_* /root/.ssh/authorized_keys
rm -rf /var/lib/cloud
sync
fstrim /
echo COORDINARE_PLATFORM_READY
poweroff
'''


def build_guest(url: str, expected: str, root: Path) -> None:
    target = root/'platform.qcow2'
    if target.exists():
        raise ValueError('platform guest output must be fresh')
    work = Path(tempfile.mkdtemp(prefix='platform-build-', dir=root))
    process = None
    try:
        source = work/'upstream.qcow2'
        digest = hashlib.sha512()
        total = 0
        with urllib.request.urlopen(url, timeout=60) as response, source.open('wb') as stream:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > 512 * 1024**2:
                    raise ValueError('platform download exceeds bound')
                digest.update(chunk)
                stream.write(chunk)
        if digest.hexdigest() != expected:
            raise ValueError('platform upstream SHA512 differs')
        config = {'users': [], 'ssh_pwauth': False, 'disable_root': False, 'ssh_genkeytypes': [], 'runcmd': [PLATFORM_SCRIPT]}
        (work/'user-data').write_text('#cloud-config\n' + json.dumps(config) + '\n')
        (work/'meta-data').write_text('instance-id: coordinare-platform-build\n')
        subprocess.run(['/usr/bin/genisoimage', '-quiet', '-output', str(work/'seed.iso'), '-volid', 'cidata', '-joliet', '-rock',
                        str(work/'user-data'), str(work/'meta-data')], check=True, timeout=30)
        subprocess.run(['/usr/bin/qemu-img', 'create', '-q', '-f', 'qcow2', '-F', 'qcow2', '-b', str(source), str(work/'working.qcow2'), '2147483648'],
                       check=True, timeout=30)
        with (work/'serial.log').open('wb') as log:
            process = subprocess.Popen(['/usr/bin/qemu-system-x86_64', '-accel', 'tcg', '-m', '2048', '-smp', '2',
                '-bios', '/usr/share/seabios/bios-256k.bin',
                '-drive', f'file={work}/working.qcow2,format=qcow2,if=virtio,discard=unmap',
                '-drive', f'file={work}/seed.iso,format=raw,media=cdrom,if=virtio,readonly=on',
                '-netdev', 'user,id=net0', '-device', 'virtio-net-pci,netdev=net0,romfile=',
                '-nographic', '-no-reboot'], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            code = process.wait(timeout=900)
        serial = (work/'serial.log').read_text(errors='replace')
        if code != 0 or 'COORDINARE_PLATFORM_READY' not in serial:
            raise RuntimeError('generic platform preparation failed')
        subprocess.run(['/usr/bin/qemu-img', 'convert', '-O', 'qcow2', str(work/'working.qcow2'), str(target)], check=True, timeout=180)
        (root/'bundle.json').write_text(json.dumps({
            'schema_version': 1, 'guest_platform': 'linux/x86_64', 'guest_image': str(target),
            'emulator': '/usr/bin/qemu-system-x86_64', 'firmware': '/usr/share/seabios/bios-256k.bin',
        }, sort_keys=True) + '\n')
        (root/'upstream.json').write_text(json.dumps({'url': url, 'sha512': expected, 'bytes': total}) + '\n')
    except BaseException:
        target.unlink(missing_ok=True)
        if (work/'serial.log').exists():
            # Bounded platform-only log, containing no project or provider inputs.
            sys.stderr.write((work/'serial.log').read_text(errors='replace')[-12000:] + '\n')
        raise
    finally:
        if process is not None and process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        shutil.rmtree(work)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--sha512', required=True)
    parser.add_argument('--root', type=Path, default=Path('/opt/coordinare-runtime'))
    args = parser.parse_args()
    build_guest(args.url, args.sha512, args.root)


if __name__ == '__main__':
    main()
