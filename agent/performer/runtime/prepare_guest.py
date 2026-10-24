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


# A guest matches its carrier's CPU, so it runs without emulation; the other CPU is reached through the static
# interpreter that the guest keeps and that binfmt_misc registers at boot (see runtime_guest.binfmt_entry).
GUESTS = {
    'x86_64': {'platform': 'linux/x86_64', 'emulator': '/usr/bin/qemu-system-x86_64',
               'firmware': '/usr/share/seabios/bios-256k.bin', 'machine': [], 'console': '/dev/ttyS0',
               'packages': 'qemu-system-x86_64 seabios'},
    'aarch64': {'platform': 'linux/aarch64', 'emulator': '/usr/bin/qemu-system-aarch64',
                'firmware': '/usr/share/qemu-efi-aarch64/QEMU_EFI.fd', 'machine': ['-M', 'virt', '-cpu', 'cortex-a72'],
                'console': '/dev/ttyAMA0', 'packages': ''},
}
FOREIGN = {'x86_64': 'aarch64', 'aarch64': 'x86_64'}
INTERPRETER_DIR = '/usr/local/lib/coordinare'

PLATFORM_SCRIPT = '''#!/bin/sh
exec >@CONSOLE@ 2>&1
set -eu
trap 'status=$?; if [ $status -ne 0 ]; then echo COORDINARE_PLATFORM_FAILED:$status; poweroff -f; fi' EXIT
mount -t tmpfs -o mode=1777,size=512m tmpfs /tmp
apk add --no-cache docker docker-cli-compose openssh-server python3 e2fsprogs @PACKAGES@
modprobe 9pnet_virtio
mkdir -p /mnt/payload @INTERPRETER_DIR@
mount -t 9p -o trans=virtio,version=9p2000.L,ro payload /mnt/payload
install -m 0755 /mnt/payload/@FOREIGN@ @INTERPRETER_DIR@/@FOREIGN@
umount /mnt/payload
test -x @INTERPRETER_DIR@/@FOREIGN@
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


def platform_script(arch: str, foreign: str) -> str:
    return (PLATFORM_SCRIPT.replace('@CONSOLE@', GUESTS[arch]['console']).replace('@PACKAGES@', GUESTS[arch]['packages'])
            .replace('@INTERPRETER_DIR@', INTERPRETER_DIR)
            .replace('@FOREIGN@', foreign))


def build_guest(url: str, expected: str, root: Path, arch: str) -> None:
    profile = GUESTS[arch]
    foreign = FOREIGN[arch]
    root.mkdir(parents=True, exist_ok=True)
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
        payload = work/'payload'
        payload.mkdir()
        shutil.copyfile(f'/usr/bin/qemu-{foreign}-static', payload/f'qemu-{foreign}-static')
        config = {'users': [], 'ssh_pwauth': False, 'disable_root': False, 'ssh_genkeytypes': [], 'runcmd': [platform_script(arch, f'qemu-{foreign}-static')]}
        (work/'user-data').write_text('#cloud-config\n' + json.dumps(config) + '\n')
        (work/'meta-data').write_text('instance-id: coordinare-platform-build\n')
        subprocess.run(['/usr/bin/genisoimage', '-quiet', '-output', str(work/'seed.iso'), '-volid', 'cidata', '-joliet', '-rock',
                        str(work/'user-data'), str(work/'meta-data')], check=True, timeout=30)
        subprocess.run(['/usr/bin/qemu-img', 'create', '-q', '-f', 'qcow2', '-F', 'qcow2', '-b', str(source), str(work/'working.qcow2'), '2147483648'],
                       check=True, timeout=30)
        with (work/'serial.log').open('wb') as log:
            process = subprocess.Popen([profile['emulator'], '-accel', 'tcg', *profile['machine'], '-m', '2048', '-smp', '2',
                '-bios', profile['firmware'],
                '-drive', f'file={work}/working.qcow2,format=qcow2,if=virtio,discard=unmap',
                '-drive', f'file={work}/seed.iso,format=raw,media=cdrom,if=virtio,readonly=on',
                '-netdev', 'user,id=net0', '-device', 'virtio-net-pci,netdev=net0,romfile=',
                '-virtfs', f'local,path={payload},mount_tag=payload,security_model=none,readonly=on',
                '-nographic', '-no-reboot'], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            code = process.wait(timeout=900)
        serial = (work/'serial.log').read_text(errors='replace')
        if code != 0 or 'COORDINARE_PLATFORM_READY' not in serial:
            raise RuntimeError('generic platform preparation failed')
        subprocess.run(['/usr/bin/qemu-img', 'convert', '-O', 'qcow2', str(work/'working.qcow2'), str(target)], check=True, timeout=180)
        (root/'bundle.json').write_text(json.dumps({
            'schema_version': 1, 'guest_platform': profile['platform'], 'guest_image': str(target),
            'emulator': profile['emulator'], 'firmware': profile['firmware'],
        }, sort_keys=True) + '\n')
        interpreter = payload/f'qemu-{foreign}-static'
        (root/'upstream.json').write_text(json.dumps({'url': url, 'sha512': expected, 'bytes': total, 'foreign': {
            'interpreter': f'qemu-{foreign}-static', 'sha256': hashlib.sha256(interpreter.read_bytes()).hexdigest()}}) + '\n')
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
    parser.add_argument('--arch', required=True, choices=sorted(GUESTS))
    parser.add_argument('--root', type=Path, required=True, help='per-guest directory, e.g. /opt/coordinare-runtime/aarch64')
    args = parser.parse_args()
    build_guest(args.url, args.sha512, args.root, args.arch)


if __name__ == '__main__':
    main()
