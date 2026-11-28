"""Behavior tests for agent/performer/entrypoint.sh.

The entrypoint is shell, so we exercise it as a subprocess with stubbed
external commands (rtk, npm, curl, python) on PATH. Each stub logs its
invocation to a temp file; assertions check what was called.

Covers spec 062 Fix 2 — RTK_ENABLED gate behaviour.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "agent" / "performer" / "entrypoint.sh"


def _make_stubs(tmp_path: Path) -> tuple[Path, Path]:
    """Create a stub-bin dir on PATH; return (bindir, logfile)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    logfile = tmp_path / "calls.log"
    for name in ("rtk", "npm", "curl", "python"):
        stub = bindir / name
        stub.write_text(
            f'#!/bin/sh\necho "{name} $*" >> "{logfile}"\nexit 0\n',
        )
        stub.chmod(0o755)
    return bindir, logfile


def _run(bindir: Path, env_extra: dict[str, str]) -> tuple[int, str, str]:
    # HOME points at tmp_path so the entrypoint's `mkdir -p $HOME/.claude`
    # lands in the test sandbox, not the real user home.
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(bindir.parent)}
    env.update(env_extra)
    result = subprocess.run(
        ["sh", str(ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode, result.stdout, result.stderr


def _read_log(logfile: Path) -> str:
    return logfile.read_text() if logfile.exists() else ""


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present",
)
def test_entrypoint_skips_rtk_when_disabled(tmp_path):
    """Default (RTK_ENABLED unset) MUST NOT call rtk."""
    bindir, logfile = _make_stubs(tmp_path)
    rc, _out, _err = _run(bindir, {"BACKEND": "codex"})
    assert rc == 0
    log = _read_log(logfile)
    assert "rtk" not in log, f"rtk should not have been called; log:\n{log}"
    assert "python -m performer" in log


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present",
)
def test_entrypoint_skips_rtk_init_for_codex_with_info_log(tmp_path):
    """Codex has no rtk auto-hook upstream; we log INFO and skip init."""
    bindir, logfile = _make_stubs(tmp_path)
    rc, _out, err = _run(bindir, {"BACKEND": "codex", "RTK_ENABLED": "1"})
    assert rc == 0
    log = _read_log(logfile)
    assert "rtk init" not in log, f"rtk init must not run for codex; log:\n{log}"
    assert "no codex auto-hook" in err


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present",
)
def test_entrypoint_runs_rtk_init_for_claude(tmp_path):
    bindir, logfile = _make_stubs(tmp_path)
    rc, _out, _err = _run(bindir, {"BACKEND": "claude", "RTK_ENABLED": "1"})
    assert rc == 0
    log = _read_log(logfile)
    assert "rtk init -g" in log, f"expected rtk init -g; log:\n{log}"


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present",
)
def test_entrypoint_creates_claude_dir_before_rtk_init(tmp_path):
    """rtk init -g requires $HOME/.claude to pre-exist; entrypoint must mkdir."""
    bindir, _logfile = _make_stubs(tmp_path)
    home = bindir.parent
    claude_dir = home / ".claude"
    assert not claude_dir.exists()
    rc, _out, _err = _run(bindir, {"BACKEND": "claude", "RTK_ENABLED": "1"})
    assert rc == 0
    assert claude_dir.is_dir(), "entrypoint should have created $HOME/.claude"


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present",
)
def test_entrypoint_warns_and_skips_rtk_for_unsupported_backend(tmp_path):
    """Unsupported backends with RTK_ENABLED=1 warn but don't call rtk."""
    bindir, logfile = _make_stubs(tmp_path)
    rc, _out, err = _run(bindir, {"BACKEND": "opencode", "RTK_ENABLED": "1"})
    assert rc == 0
    log = _read_log(logfile)
    assert "rtk init" not in log, f"rtk should not be called; log:\n{log}"
    assert "no supported rtk hook" in err


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present",
)
def test_entrypoint_warns_when_rtk_enabled_without_backend(tmp_path):
    bindir, logfile = _make_stubs(tmp_path)
    rc, _out, err = _run(bindir, {"RTK_ENABLED": "1"})
    assert rc == 0
    log = _read_log(logfile)
    assert "rtk init" not in log
    assert "BACKEND unset" in err


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present",
)
def test_entrypoint_rtk_failure_is_fatal_when_enabled(tmp_path):
    """If rtk init exits non-zero with RTK_ENABLED=1, entrypoint MUST fail.

    Silent fallback to no-compression defeats the explicit opt-in and makes
    A/B comparisons meaningless.
    """
    bindir, logfile = _make_stubs(tmp_path)
    # Replace rtk stub with one that fails.
    (bindir / "rtk").write_text(
        f'#!/bin/sh\necho "rtk $*" >> "{logfile}"\nexit 1\n',
    )
    (bindir / "rtk").chmod(0o755)

    rc, _out, err = _run(bindir, {"BACKEND": "claude", "RTK_ENABLED": "1"})
    assert rc != 0
    log = _read_log(logfile)
    assert "python -m performer" not in log
    assert "rtk init failed" in err


# ---- Egress allowlist behaviour ----


def _make_egress_stubs(tmp_path: Path) -> tuple[Path, Path]:
    """Stub bin dir that also includes iptables/ip6tables/getent.

    getent emits parseable ahosts output for known hosts and exits non-zero
    for ``unresolvable.example`` so the warn-and-skip path is exercised.
    """
    bindir, logfile = _make_stubs(tmp_path)
    for tool in ("iptables", "ip6tables"):
        stub = bindir / tool
        stub.write_text(
            f'#!/bin/sh\necho "{tool} $*" >> "{logfile}"\nexit 0\n',
        )
        stub.chmod(0o755)
    getent = bindir / "getent"
    getent.write_text(
        f"""#!/bin/sh
echo "getent $*" >> "{logfile}"
db="$1"
host="$2"
case "$host" in
  unresolvable.example) exit 2 ;;
esac
case "$db" in
  ahostsv4) echo "140.82.114.6    STREAM $host"; echo "140.82.114.6    DGRAM"; echo "140.82.114.6    RAW" ;;
  ahostsv6) echo "2606:50c0:8000::153    STREAM $host" ;;
esac
exit 0
""",
    )
    getent.chmod(0o755)
    return bindir, logfile


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present",
)
def test_entrypoint_egress_skipped_when_unset(tmp_path):
    """Default (PERFORMER_EGRESS_ALLOWLIST unset) MUST NOT call iptables."""
    bindir, logfile = _make_egress_stubs(tmp_path)
    rc, _out, _err = _run(bindir, {"BACKEND": "codex"})
    assert rc == 0
    log = _read_log(logfile)
    assert "iptables" not in log
    assert "ip6tables" not in log
    assert "getent" not in log
    assert "python -m performer" in log


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present",
)
def test_entrypoint_egress_applies_rules_for_each_host(tmp_path):
    """Allowlist resolves each host and adds v4+v6 ACCEPT rules, then sets DROP."""
    bindir, logfile = _make_egress_stubs(tmp_path)
    rc, _out, err = _run(
        bindir,
        {
            "BACKEND": "codex",
            "PERFORMER_EGRESS_ALLOWLIST": "api.github.com, objects.githubusercontent.com",
        },
    )
    assert rc == 0, err
    log = _read_log(logfile)
    # baseline rules
    assert "iptables -F OUTPUT" in log
    assert "iptables -A OUTPUT -o lo -j ACCEPT" in log
    assert "iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT" in log
    assert "iptables -A OUTPUT -p udp --dport 53 -j ACCEPT" in log
    # per-host ACCEPT (v4 + v6) — also proves whitespace in CSV is trimmed
    assert "iptables -A OUTPUT -d 140.82.114.6 -j ACCEPT" in log
    assert "ip6tables -A OUTPUT -d 2606:50c0:8000::153 -j ACCEPT" in log
    # default-drop applied last
    assert "iptables -P OUTPUT DROP" in log
    assert "ip6tables -P OUTPUT DROP" in log
    # python still launched
    assert "python -m performer" in log


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present",
)
def test_entrypoint_egress_warns_on_unresolvable_host_and_continues(tmp_path):
    """Unresolvable hosts log WARNING but do not abort the container."""
    bindir, logfile = _make_egress_stubs(tmp_path)
    rc, _out, err = _run(
        bindir,
        {
            "BACKEND": "codex",
            "PERFORMER_EGRESS_ALLOWLIST": "unresolvable.example,api.github.com",
        },
    )
    assert rc == 0, err
    assert "no usable IPs for egress-allowlist host 'unresolvable.example'" in err
    log = _read_log(logfile)
    # resolvable host still got its rule
    assert "iptables -A OUTPUT -d 140.82.114.6 -j ACCEPT" in log
    assert "python -m performer" in log


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present",
)
def test_entrypoint_egress_fails_without_iptables(tmp_path):
    """PERFORMER_EGRESS_ALLOWLIST without iptables on PATH MUST refuse to start."""
    bindir, _logfile = _make_egress_stubs(tmp_path)
    (bindir / "iptables").unlink()
    rc, _out, err = _run(
        bindir,
        {"BACKEND": "codex", "PERFORMER_EGRESS_ALLOWLIST": "api.github.com"},
    )
    assert rc != 0
    assert "iptables missing" in err


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present",
)
def test_entrypoint_egress_warns_when_ip6tables_absent_but_continues(tmp_path):
    """Missing ip6tables warns and proceeds with v4-only enforcement."""
    bindir, logfile = _make_egress_stubs(tmp_path)
    (bindir / "ip6tables").unlink()
    rc, _out, err = _run(
        bindir,
        {"BACKEND": "codex", "PERFORMER_EGRESS_ALLOWLIST": "api.github.com"},
    )
    assert rc == 0, err
    assert "ip6tables unavailable" in err
    log = _read_log(logfile)
    assert "ip6tables" not in log  # stub was removed; nothing called it
    assert "iptables -P OUTPUT DROP" in log
    assert "python -m performer" in log


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present",
)
def test_entrypoint_egress_fails_when_iptables_unusable(tmp_path):
    """iptables present but `-L OUTPUT -n` fails (e.g. no NET_ADMIN) → refuse."""
    bindir, logfile = _make_egress_stubs(tmp_path)
    # Replace iptables stub with one that succeeds on `command -v` discovery
    # but fails the health check (mirrors the missing-NET_ADMIN branch).
    (bindir / "iptables").write_text(
        f"""#!/bin/sh
echo "iptables $*" >> "{logfile}"
case "$1" in
  -L) exit 3 ;;
esac
exit 0
""",
    )
    (bindir / "iptables").chmod(0o755)
    rc, _out, err = _run(
        bindir,
        {"BACKEND": "codex", "PERFORMER_EGRESS_ALLOWLIST": "api.github.com"},
    )
    assert rc != 0
    assert "iptables unusable" in err
    log = _read_log(logfile)
    # Health check ran; rule programming did NOT.
    assert "iptables -L OUTPUT -n" in log
    assert "iptables -P OUTPUT DROP" not in log
    assert "python -m performer" not in log


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present",
)
def test_entrypoint_egress_sets_drop_policy_after_accept_rules(tmp_path):
    """DROP policy MUST be the last rule operation — otherwise we'd lock out
    our own ACCEPT additions briefly. Catches future reorderings."""
    bindir, logfile = _make_egress_stubs(tmp_path)
    rc, _out, _err = _run(
        bindir,
        {"BACKEND": "codex", "PERFORMER_EGRESS_ALLOWLIST": "api.github.com"},
    )
    assert rc == 0
    log = _read_log(logfile)
    drop_idx = log.find("iptables -P OUTPUT DROP")
    last_accept_idx = log.rfind("iptables -A OUTPUT")
    assert drop_idx != -1 and last_accept_idx != -1
    assert drop_idx > last_accept_idx, (
        f"DROP policy must come after all ACCEPT rules; log:\n{log}"
    )


# ---- 449: the egress proxy, a Docker performer's only way out ----

_PROXY_ENV = {
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy", "NODE_USE_ENV_PROXY",
}


def _run_command(bindir: Path, env_extra: dict[str, str], *command: str) -> tuple[int, str, str]:
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(bindir.parent), "PERFORMER_PYTHON": sys.executable}
    env.update(env_extra)
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), *command], env=env, capture_output=True, text=True, timeout=20, check=False,
    )
    return result.returncode, result.stdout, result.stderr


def _environment(stdout: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)


@pytest.fixture
def listening_proxy():
    """A port that accepts connections (the kernel completes them from the backlog)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_entrypoint_with_an_egress_proxy_points_every_client_at_it_before_the_backend_install(tmp_path, listening_proxy):
    bindir, logfile = _make_egress_stubs(tmp_path)
    rc, out, err = _run_command(
        bindir, {"BACKEND": "codex", "PERFORMER_EGRESS_PROXY": listening_proxy, "HTTPS_PROXY": "http://corp.example:8080"},
        "env",
    )
    assert rc == 0, err
    env = _environment(out)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert env[name] == listening_proxy, name  # an operator's upstream proxy is replaced
    assert env["NO_PROXY"] == env["no_proxy"] == "localhost,127.0.0.1,::1"
    assert env["NODE_USE_ENV_PROXY"] == "1"
    log = _read_log(logfile)
    assert "npm install" in log  # the backend install still runs, now through the proxy
    assert "iptables" not in log  # enforcement is outside the container; no NET_ADMIN needed
    assert "getent" not in log


def test_entrypoint_refuses_to_start_when_its_egress_proxy_does_not_answer(tmp_path):
    """Fail closed: the performer never becomes ready, so the dispatch fails and is retried."""
    bindir, logfile = _make_egress_stubs(tmp_path)
    rc, _out, err = _run_command(
        bindir,
        {
            "BACKEND": "codex",
            "PERFORMER_EGRESS_PROXY": f"http://127.0.0.1:{_closed_port()}",
            "PERFORMER_EGRESS_PROXY_WAIT_SECONDS": "1",
        },
    )
    assert rc == 1
    assert "did not answer" in err
    assert _read_log(logfile) == ""  # no install, no server: nothing ran without its way out


def test_entrypoint_refuses_the_proxy_and_the_in_container_allowlist_together(tmp_path, listening_proxy):
    bindir, logfile = _make_egress_stubs(tmp_path)
    rc, _out, err = _run_command(
        bindir,
        {"BACKEND": "codex", "PERFORMER_EGRESS_PROXY": listening_proxy, "PERFORMER_EGRESS_ALLOWLIST": "api.github.com"},
    )
    assert rc == 1
    assert "PERFORMER_EGRESS_ALLOWLIST" in err
    assert _read_log(logfile) == ""


def test_entrypoint_without_an_egress_proxy_sets_no_proxy_variables(tmp_path):
    bindir, _logfile = _make_egress_stubs(tmp_path)
    rc, out, err = _run_command(bindir, {"BACKEND": "codex"}, "env")
    assert rc == 0, err
    assert not (_PROXY_ENV & set(_environment(out)))
