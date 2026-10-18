"""Diagnostic guest driver executed only inside an owned outer fixture container."""
from __future__ import annotations

import contextlib
import http.server
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path("/scratch")
CONTROL = ROOT / "control"
SOURCE = Path("/work/source-a" if sys.argv[1] == "installer" else "/work/relocated/source-b")
CONFIG = json.loads(Path("/probe-config.json").read_text())
DEADLINE = time.monotonic() + CONFIG["deadline_seconds"]
BASE_IMAGE = "busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
INVENTORY_PROGRAM = Path(__file__).with_name("private_runtime_probe_source.py").read_text() + "\nimport json,sys\nfrom pathlib import Path\nprint(json.dumps(inventory(Path(sys.argv[1])),sort_keys=True))\n"


class CanaryHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"carrier-only\n")

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def command(argv: list[str], *, seconds: float = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, capture_output=True, text=True,
                            timeout=max(0.1, min(seconds, DEADLINE - time.monotonic())))
    with Path("/evidence/commands.log").open("a") as log:
        log.write(json.dumps({"argv": argv, "exit_code": result.returncode,
                              "stdout": result.stdout[-16384:], "stderr": result.stderr[-16384:]}) + "\n")
    if check and result.returncode:
        raise RuntimeError("fixture_command_failed")
    return result


def ssh(script: str, *, seconds: float = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    return command(["/usr/bin/ssh", "-F", "/root/.ssh/config", "probe", script], seconds=seconds, check=check)


def cloud_config(install: bool) -> str:
    mounts = []
    for tag, path, readonly in [("source", str(SOURCE), False), ("cache", "/cache/artifacts", not install),
                                ("project", "/scratch/project", False), ("control", "/run/probe-control", True)]:
        mounts.append(f"mkdir -p {path}; mount -t 9p -o trans=virtio,version=9p2000.L{',ro' if readonly else ''} {tag} {path}")
    startup = "\n".join([
        "#!/bin/sh", "set -eu", "exec >>/var/log/coordinare-fixture.log 2>&1",
        "mount -t tmpfs -o mode=1777,size=512m tmpfs /tmp",
        *( ["apk add --no-cache docker docker-cli-compose openssh-server e2fsprogs"
            + (" qemu-system-x86_64 seabios" if CONFIG.get("scenario") == "nested_kernel" else "")] if install else [] ),
        "modprobe 9pnet_virtio", *mounts,
        "rc-service cgroups start", "rc-service docker start", "rc-service sshd stop || true",
        "passwd -d root", "mkdir -p /run/sshd",
        "/usr/sbin/sshd -h /run/probe-control/host-key -o PermitRootLogin=yes -o PasswordAuthentication=no -o AuthorizedKeysFile=/run/probe-control/authorized_keys",
        "for i in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 1; done",
        "docker info >/dev/null", "touch /run/coordinare-fixture-ready",
    ])
    return "#cloud-config\n" + json.dumps({
        "disable_root": False, "ssh_pwauth": False, "ssh_genkeytypes": [],
        "write_files": [{"path": "/run/coordinare-fixture-start", "permissions": "0700", "content": startup}],
        "runcmd": [["sh", "/run/coordinare-fixture-start"]],
    })


def prepare(install: bool) -> list[str]:
    CONTROL.mkdir(parents=True, mode=0o700)
    (ROOT / "project").mkdir()
    SOURCE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree("/fixture", SOURCE, symlinks=True)
    command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(CONTROL / "client-key")])
    guest_control = CONTROL / "guest"
    guest_control.mkdir()
    command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(guest_control / "host-key")])
    shutil.copyfile(CONTROL / "client-key.pub", guest_control / "authorized_keys")
    Path("/root/.ssh").mkdir(mode=0o700, exist_ok=True)
    host_public = (guest_control / "host-key.pub").read_text().strip()
    (CONTROL / "known-hosts").write_text("[127.0.0.1]:2222 " + host_public + "\n")
    Path("/root/.ssh/config").write_text(
        "Host probe 127.0.0.1\n HostName 127.0.0.1\n Port 2222\n User root\n"
        f" IdentityFile {CONTROL}/client-key\n UserKnownHostsFile {CONTROL}/known-hosts\n"
        " StrictHostKeyChecking yes\n IdentitiesOnly yes\n BatchMode yes\n PasswordAuthentication no\n ConnectTimeout 2\n LogLevel ERROR\n",
    )
    (CONTROL / "user-data").write_text(cloud_config(install))
    (CONTROL / "meta-data").write_text("instance-id: fixture-" + str(time.time_ns()) + "\n")
    command(["genisoimage", "-quiet", "-output", str(CONTROL / "seed.iso"), "-volid", "cidata", "-joliet", "-rock",
             str(CONTROL / "user-data"), str(CONTROL / "meta-data")])
    disk = Path("/cache/install-working.qcow2") if install else ROOT / "consumer.qcow2"
    base = "/base/platform.qcow2" if install else "/cache/installed.qcow2"
    command(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", base, str(disk), str(CONFIG["cache_bytes"])])
    args = ["qemu-system-" + CONFIG["architecture"], "-accel", "tcg", "-m", str(CONFIG["memory_mib"] - 512),
            "-smp", str(CONFIG["cpus"]), "-drive", f"file={disk},format=qcow2,if=virtio,discard=unmap",
            "-drive", f"file={CONTROL}/seed.iso,format=raw,media=cdrom,if=virtio,readonly=on",
            "-netdev", "user,id=net0,restrict=" + ("off" if install else "on") + ",hostfwd=tcp:127.0.0.1:2222-:22,hostfwd=tcp:127.0.0.1:8080-:8080",
            "-device", "virtio-net-pci,netdev=net0,romfile=", "-nographic", "-no-reboot"]
    if CONFIG["architecture"] == "aarch64":
        args += ["-M", "virt", "-cpu", "cortex-a72", "-bios", "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd"]
    for tag, path, readonly in [("source", SOURCE, False), ("cache", Path("/cache/artifacts"), not install),
                                ("project", ROOT / "project", False), ("control", guest_control, True)]:
        args += ["-virtfs", f"local,path={path},mount_tag={tag},security_model=none" + (",readonly=on" if readonly else "")]
    return args


def exercise(install: bool, result: dict[str, Any]) -> dict[str, Any]:
    os.environ["DOCKER_HOST"] = "ssh://root@127.0.0.1:2222"
    command(["docker", "info"], seconds=30)
    if install:
        command(["docker", "pull", BASE_IMAGE], seconds=180)
    result["source_root"] = str(SOURCE)
    result["guest_architecture"] = ssh("uname -m").stdout.strip()
    expected = json.loads(command([sys.executable, "-c", INVENTORY_PROGRAM, "/fixture"]).stdout)
    observed = json.loads(ssh("python3 -c " + shlex.quote(INVENTORY_PROGRAM) + " " + str(SOURCE)).stdout)
    result["source_identity_verified"] = expected == observed
    result["source_entries"] = observed
    result["executable_source_response"] = ssh(str(SOURCE / "show-message")).stdout
    # Establish a live denied destination, rather than mistaking a missing listener for isolation.
    result["canary_outer_response"] = command(["curl", "-fsS", "http://127.0.0.1:18081/"]).stdout
    control = ssh("wget -T 3 -qO- http://10.0.2.2:18081/", check=False)
    result["carrier_canary_exit_code"] = control.returncode
    result["carrier_canary_response"] = control.stdout
    if (install and (control.returncode != 0 or control.stdout != "carrier-only\n")) or (not install and control.returncode == 0):
        result["reason"] = "carrier_endpoint_isolation_failed" if not install else "carrier_canary_control_unavailable"
        raise RuntimeError("carrier_endpoint_test_failed")
    result["proxy_outer_response"] = command(["curl", "-fsS", "http://127.0.0.1:4000/"]).stdout
    proxy = ssh("wget -T 3 -qO- http://10.0.2.2:4000/", check=False)
    result["proxy_canary_exit_code"] = proxy.returncode
    result["proxy_canary_response"] = proxy.stdout
    Path("/outer-only-marker").write_text("not-exported\n")
    (ROOT / "project" / "escape").symlink_to("/outer-only-marker")
    escaped = ssh("cat /scratch/project/escape", check=False)
    result["parent_export_denied"] = escaped.returncode != 0 and "not-exported" not in escaped.stdout
    known_hosts = CONTROL / "known-hosts"
    original = known_hosts.read_text()
    try:
        known_hosts.write_text("[127.0.0.1]:2222 " + (CONTROL / "client-key.pub").read_text())
        rejected = ssh("true", check=False)
        result["host_key_mismatch_rejected"] = rejected.returncode == 255 and "Host key verification failed" in rejected.stderr
    finally:
        known_hosts.write_text(original)
    built = command(["docker", "build", "--pull=false", "--network=none", "-t", "coordinare-runtime-fixture:local", str(SOURCE)], seconds=180)
    if not install:
        result["offline_rebuild_exit_code"] = built.returncode
        result["missing_image_exit_code"] = command(
            ["docker", "run", "--rm", "--pull=never", "coordinare-fixture-absent:local"], check=False,
        ).returncode
        result["outer_cache_write_exit_code"] = command(["sh", "-c", "echo forbidden >/cache/artifacts/forbidden"], check=False).returncode
        result["guest_cache_write_exit_code"] = ssh("echo forbidden >/cache/artifacts/forbidden", check=False).returncode
    if CONFIG.get("scenario") == "buildkit":
        cached = command(["docker", "build", "--progress=plain", "--pull=false", "--network=none",
                          "--build-arg", "PHASE=" + ("installer" if install else "consumer"),
                          "-f", str(SOURCE / "cache.Dockerfile"), "-t", "coordinare-buildkit-fixture:local", str(SOURCE)], seconds=180)
        result["buildkit_cache_exit_code"] = cached.returncode
    command(["docker", "run", "-d", "--name", "fixture-http", "-p", "8080:8080", "coordinare-runtime-fixture:local"])
    for _ in range(40):
        response = command(["curl", "-fsS", "--max-time", "2", "http://127.0.0.1:8080/message.txt"], check=False)
        if response.returncode == 0:
            result["docker_response"] = response.stdout
            break
        time.sleep(0.5)
    command(["docker", "rm", "-f", "fixture-http"])
    result["bind_mount_response"] = command(
        ["docker", "run", "--rm", "--network=none", "-v", str(SOURCE) + ":/src:ro", BASE_IMAGE,
         "sh", "-c", "test -f /src/message.txt && echo fresh-bind-source"],
    ).stdout
    compose = ["docker", "compose", "-f", str(SOURCE / "compose.yaml")]
    command([*compose, "up", "-d", "--no-build", "--pull", "never"])
    response = command(["curl", "-fsS", "--retry", "15", "--retry-connrefused", "--retry-delay", "1", "--max-time", "20", "http://127.0.0.1:8080/message.txt"])
    result["compose_response"] = response.stdout
    result["service_cleanup_exit_code"] = command([*compose, "down", "--volumes", "--remove-orphans"]).returncode
    return result


def hold_until_cancelled(mode: str) -> None:
    """Reach a real blocking operation before announcing the cancellation point."""
    os.environ["DOCKER_HOST"] = "ssh://root@127.0.0.1:2222"
    worker: subprocess.Popen[bytes] | None = None
    try:
        if mode == "build":
            context = ROOT / "project" / "build-cancel"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM " + BASE_IMAGE + "\nRUN echo CANCELLATION_BUILD_ACTIVE; sleep 120\n")
            with Path("/evidence/build-cancel.log").open("wb") as log:
                worker = subprocess.Popen(["/usr/bin/docker", "build", "--pull=false", str(context)], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                while time.monotonic() < DEADLINE and worker.poll() is None:
                    lines = Path("/evidence/build-cancel.log").read_bytes().splitlines()
                    if any(line.strip() == b"CANCELLATION_BUILD_ACTIVE" or
                           (line.startswith(b"#") and line.split(maxsplit=2)[-1] == b"CANCELLATION_BUILD_ACTIVE") for line in lines):
                        break
                    time.sleep(0.2)
                else:
                    raise RuntimeError("build_cancellation_point_not_reached")
        elif mode == "health":
            command(["docker", "run", "-d", "--name", "fixture-health", BASE_IMAGE, "sleep", "120"])
            failed = command(["curl", "-fsS", "--max-time", "2", "http://127.0.0.1:8080/"], check=False)
            if failed.returncode == 0:
                raise RuntimeError("health_cancellation_point_not_reached")
        elif mode == "stop":
            command(["docker", "run", "-d", "--name", "fixture-stop", "-v", "/scratch/project:/signal", BASE_IMAGE,
                     "sh", "-c", "trap 'echo received >/signal/term' TERM; echo ready >/signal/ready; while :; do sleep 1; done"])
            while not (ROOT / "project" / "ready").exists() and time.monotonic() < DEADLINE:
                time.sleep(0.2)
            worker = subprocess.Popen(["/usr/bin/docker", "stop", "-t", "120", "fixture-stop"], stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True)
            while time.monotonic() < DEADLINE and worker.poll() is None:
                if (ROOT / "project" / "term").exists():
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("stop_cancellation_point_not_reached")
        Path("/evidence/cancel-ready.json").write_text(json.dumps({"phase": mode, "operation_active": True}))
        while time.monotonic() < DEADLINE:
            time.sleep(0.5)
        raise RuntimeError("cancellation_not_delivered")
    finally:
        if worker is not None and worker.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(worker.pid, signal.SIGTERM)
            try:
                worker.wait(timeout=3)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(worker.pid, signal.SIGKILL)
                worker.wait(timeout=3)


def nested_kernel(result: dict[str, Any]) -> None:
    """Boot a synthetic Linux initramfs in a nested unprivileged container."""
    script = """set -eu
mkdir -p /tmp/nested-init/bin /tmp/nested-init/lib /tmp/nested-init/dev /tmp/nested-init/proc /scratch/project/nested
cp /bin/busybox /tmp/nested-init/bin/busybox
cp -L /lib/ld-musl-x86_64.so.1 /tmp/nested-init/lib/ld-musl-x86_64.so.1
ln -s ld-musl-x86_64.so.1 /tmp/nested-init/lib/libc.musl-x86_64.so.1
mknod /tmp/nested-init/dev/console c 5 1
mknod /tmp/nested-init/dev/null c 1 3
cat >/tmp/nested-init/init <<'NESTED_INIT'
#!/bin/busybox sh
set -e
/bin/busybox mount -t proc proc /proc
/bin/busybox uname -m
echo NESTED_LINUX_WORKS
/bin/busybox poweroff -f
NESTED_INIT
chmod 755 /tmp/nested-init/init
cp /boot/vmlinuz-virt /scratch/project/nested/kernel
cd /tmp/nested-init
find . | cpio -o -H newc | gzip -1 >/scratch/project/nested/initrd.gz
"""
    ssh(script, seconds=60)
    started = time.monotonic()
    boot = command(["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "--memory", "1g", "--memory-swap", "1g", "--cpus", "1",
                    "-v", "/usr:/usr:ro", "-v", "/lib:/lib:ro", "-v", "/scratch/project/nested:/input:ro", BASE_IMAGE,
                    "/usr/bin/qemu-system-x86_64", "-accel", "tcg", "-m", "256", "-smp", "1", "-nodefaults",
                    "-display", "none", "-serial", "stdio", "-monitor", "none", "-no-reboot",
                    "-kernel", "/input/kernel", "-initrd", "/input/initrd.gz", "-append", "console=ttyS0 quiet panic=-1"],
                   seconds=180, check=False)
    result["nested_kernel_exit_code"] = boot.returncode
    result["nested_kernel_output"] = boot.stdout[-16384:]
    result["nested_kernel_seconds"] = round(time.monotonic() - started, 3)
    if boot.returncode != 0 or "NESTED_LINUX_WORKS" not in boot.stdout:
        result["reason"] = "nested_kernel_boot_failed"
        raise RuntimeError("nested_kernel_boot_failed")


def main() -> int:
    mode = sys.argv[1]
    install = mode == "installer"
    result: dict[str, Any] = {"status": "held", "reason": "guest_start_failed"}

    def terminate(_signal: int, _frame: object) -> None:
        result["cancelled"] = True
        raise InterruptedError("owned_fixture_cancelled")

    signal.signal(signal.SIGTERM, terminate)
    process: subprocess.Popen[bytes] | None = None
    canary = http.server.ThreadingHTTPServer(("127.0.0.1", 18081), CanaryHandler)
    canary_thread = threading.Thread(target=canary.serve_forever, daemon=True)
    canary_thread.start()
    proxy_canary = http.server.ThreadingHTTPServer(("127.0.0.1", 4000), CanaryHandler)
    proxy_thread = threading.Thread(target=proxy_canary.serve_forever, daemon=True)
    proxy_thread.start()
    started = time.monotonic()
    try:
        args = prepare(install)
        with Path("/evidence/serial.log").open("wb") as serial:
            process = subprocess.Popen(args, stdout=serial, stderr=subprocess.STDOUT, start_new_session=True)
            if mode == "cancel_boot":
                Path("/evidence/cancel-ready.json").write_text(json.dumps({"phase": "boot", "operation_active": process.poll() is None}))
            while time.monotonic() < DEADLINE and process.poll() is None:
                try:
                    ready = ssh("test -f /run/coordinare-fixture-ready", seconds=5, check=False)
                    if ready.returncode == 0:
                        break
                except subprocess.TimeoutExpired:
                    pass
                time.sleep(2)
            else:
                raise RuntimeError("guest_boot_deadline")
            if mode.startswith("cancel_"):
                hold_until_cancelled(mode.removeprefix("cancel_"))
            result["reason"] = "fixture_command_failed"
            result = exercise(install, result)
            if CONFIG.get("scenario") == "nested_kernel":
                nested_kernel(result)
            if not install:
                result["egress_denial"] = {
                    name: ssh(f"wget -T 3 -qO- {url}", seconds=6, check=False).returncode != 0
                    for name, url in [("external", "http://1.1.1.1"), ("carrier_host", "http://10.0.2.2:18081"),
                                      ("metadata", "http://169.254.169.254")]
                }
            if mode == "concurrent":
                command(["docker", "run", "-d", "--name", "concurrent-http", "-p", "8080:8080", "coordinare-runtime-fixture:local"])
                response = command(["curl", "-fsS", "--retry", "15", "--retry-connrefused", "--retry-delay", "1", "--max-time", "20", "http://127.0.0.1:8080/message.txt"])
                Path("/evidence/service-live.json").write_text(json.dumps({"response": response.stdout}))
                while not Path("/evidence/release").exists() and time.monotonic() < DEADLINE:
                    time.sleep(0.2)
                if not Path("/evidence/release").exists():
                    raise RuntimeError("concurrent_release_deadline")
                command(["docker", "rm", "-f", "concurrent-http"])
            if mode == "disk_pressure":
                pressure = ssh("set -eu; truncate -s 32M /tmp/fixture-fs; mkfs.ext4 -q -F /tmp/fixture-fs; "
                               "mkdir /tmp/fixture-limit; mount -o loop /tmp/fixture-fs /tmp/fixture-limit; "
                               "set +e; dd if=/dev/zero of=/tmp/fixture-limit/full bs=1M count=64 2>/tmp/fixture-dd.log; status=$?; set -e; "
                               "umount /tmp/fixture-limit; rm /tmp/fixture-fs; test $status -ne 0; "
                               "grep -q 'No space left' /tmp/fixture-dd.log; echo DISK_LIMIT_ENFORCED", seconds=90)
                result["disk_exhaustion_observed"] = pressure.stdout.strip() == "DISK_LIMIT_ENFORCED"
            # Keys live only on an ephemeral control export. Remove automatically generated guest keys/cloud state before sealing.
            ssh("set -e; rm -f /etc/ssh/ssh_host_* /root/.ssh/authorized_keys; rm -rf /var/lib/cloud; sync; fstrim /; poweroff", seconds=20, check=False)
            process.wait(timeout=30)
            if process.returncode != 0:
                raise RuntimeError("guest_shutdown_failed")
        if install:
            command(["qemu-img", "convert", "-O", "qcow2", "/cache/install-working.qcow2", "/cache/installed.qcow2"], seconds=180)
            Path("/cache/install-working.qcow2").unlink()
        result["status"] = "phase_completed"
        result.pop("reason", None)
        result["seconds"] = round(time.monotonic() - started, 3)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        result["status"] = "held"
    finally:
        if process is not None and process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        shutil.rmtree(CONTROL, ignore_errors=True)
        result["qemu_reaped"] = process is None or process.poll() is not None
        result["control_keys_absent"] = not CONTROL.exists()
        memory_events = Path("/sys/fs/cgroup/memory.events")
        if memory_events.exists():
            events = dict(line.split() for line in memory_events.read_text().splitlines())
            result["memory_exhaustion_observed"] = int(events.get("oom_kill", "0")) > 0
        peak = Path("/sys/fs/cgroup/memory.peak")
        if peak.exists():
            result["peak_memory_bytes"] = int(peak.read_text())
        cpu = Path("/sys/fs/cgroup/cpu.stat")
        if cpu.exists():
            result["cpu_usage_usec"] = int(dict(line.split() for line in cpu.read_text().splitlines()).get("usage_usec", "0"))
        result["scratch_allocated_bytes"] = sum(path.lstat().st_blocks * 512 for path in ROOT.rglob("*") if not path.is_symlink())
        with socket.socket() as listener:
            try:
                listener.bind(("127.0.0.1", 2222))
                result["control_listener_absent"] = True
            except OSError:
                result["control_listener_absent"] = False
        canary.shutdown()
        canary.server_close()
        canary_thread.join(timeout=2)
        proxy_canary.shutdown()
        proxy_canary.server_close()
        proxy_thread.join(timeout=2)
        Path("/evidence/phase.json").write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["status"] == "phase_completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
