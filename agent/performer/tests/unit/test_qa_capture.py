"""Tests for the deterministic QA screenshot backstop (performer-owned capture)."""
from __future__ import annotations

from pathlib import Path

from performer.qa_capture import capture_app_screenshot


def _writing_runner(tmp: Path):
    """Fake subprocess runner that simulates a successful Playwright capture by
    creating the output file (the last argv element)."""
    def run(cmd, **kwargs):
        out = cmd[-1]
        Path(out).write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)
        class _R:
            returncode = 0
            stdout = b"captured"
            stderr = b""
        return _R()
    return run


def test_returns_none_when_port_env_absent(tmp_path: Path) -> None:
    out = capture_app_screenshot(
        env={}, out_path=str(tmp_path / "s.png"),
        port_check=lambda h, p: True, runner=_writing_runner(tmp_path),
    )
    assert out is None  # no PORT → nothing to capture


def test_returns_none_when_app_not_serving(tmp_path: Path) -> None:
    called = {"n": 0}
    def runner(cmd, **kw):
        called["n"] += 1
    out = capture_app_screenshot(
        env={"PORT": "43000"}, out_path=str(tmp_path / "s.png"),
        port_check=lambda h, p: False, runner=runner,
    )
    assert out is None
    assert called["n"] == 0  # must NOT run Playwright when the app isn't up


def test_captures_when_serving_and_file_created(tmp_path: Path) -> None:
    out_path = tmp_path / "s.png"
    out = capture_app_screenshot(
        env={"PORT": "43000", "PROTOCOL": "http"}, out_path=str(out_path),
        port_check=lambda h, p: True, runner=_writing_runner(tmp_path),
    )
    assert out == str(out_path)
    assert out_path.is_file() and out_path.stat().st_size > 0


def test_returns_none_when_capture_produces_no_file(tmp_path: Path) -> None:
    out = capture_app_screenshot(
        env={"PORT": "43000"}, out_path=str(tmp_path / "s.png"),
        port_check=lambda h, p: True, runner=lambda cmd, **kw: None,  # runs but writes nothing
    )
    assert out is None  # never claim an artifact the capture did not create


def test_targets_the_configured_port_and_protocol(tmp_path: Path) -> None:
    seen = {}
    def runner(cmd, **kw):
        seen["url"] = cmd[-2]  # url is second-to-last argv (…, url, out)
        Path(cmd[-1]).write_bytes(b"png")
    capture_app_screenshot(
        env={"PORT": "43000", "PROTOCOL": "http"}, out_path=str(tmp_path / "s.png"),
        port_check=lambda h, p: True, runner=runner,
    )
    assert "43000" in seen["url"] and seen["url"].startswith("http://")


# --- app-start inference -----------------------------------------------------
from performer.qa_capture import (  # noqa: E402
    boot_and_capture_app_screenshot,
    infer_app_start_command,
)


def test_infer_rails_from_bin_rails(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "rails").write_text("#!/usr/bin/env ruby\n")
    cmd = infer_app_start_command(tmp_path, {"PORT": "43000", "RAILS_ENV": "test"})
    assert cmd is not None
    assert cmd[:2] == ["bin/rails", "server"]
    assert "43000" in cmd and "127.0.0.1" in cmd and "test" in cmd


def test_infer_django_from_manage_py(tmp_path: Path) -> None:
    (tmp_path / "manage.py").write_text("# django\n")
    cmd = infer_app_start_command(tmp_path, {"PORT": "8001"})
    assert cmd is not None
    assert "manage.py" in cmd and "runserver" in cmd
    assert any("8001" in part for part in cmd)


def test_infer_node_prefers_dev_then_start(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"start": "node .", "dev": "vite"}}')
    assert infer_app_start_command(tmp_path, {"PORT": "3000"}) == ["npm", "run", "dev"]
    (tmp_path / "package.json").write_text('{"scripts": {"start": "node ."}}')
    assert infer_app_start_command(tmp_path, {"PORT": "3000"}) == ["npm", "run", "start"]


def test_infer_returns_none_for_unknown_project(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hi")
    assert infer_app_start_command(tmp_path, {"PORT": "43000"}) is None


# --- boot-and-capture backstop -----------------------------------------------
def _flip_port_check(flip_after: int):
    """port_check that reports 'not serving' until called flip_after times."""
    calls = {"n": 0}
    def check(host, port):
        calls["n"] += 1
        return calls["n"] > flip_after
    return check, calls


def test_boot_captures_directly_when_already_serving(tmp_path: Path) -> None:
    spawned = {"n": 0}
    out = boot_and_capture_app_screenshot(
        env={"PORT": "43000"}, workspace=tmp_path, out_path=str(tmp_path / "s.png"),
        port_check=lambda h, p: True,
        runner=_writing_runner(tmp_path),
        spawner=lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1),
        sleep=lambda s: None,
    )
    assert out == str(tmp_path / "s.png")
    assert spawned["n"] == 0  # already up → must NOT launch a server


def test_boot_launches_infers_polls_then_captures(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "rails").write_text("#!/usr/bin/env ruby\n")
    check, _ = _flip_port_check(flip_after=2)  # not serving on first checks, then up
    terminated = {"n": 0}
    class _Proc:
        def terminate(self): terminated["n"] += 1
        def wait(self, timeout=None): return 0
        def poll(self): return None
    spawn_args = {}
    def spawner(cmd, **kw):
        spawn_args["cmd"] = cmd
        spawn_args["cwd"] = kw.get("cwd")
        return _Proc()
    out = boot_and_capture_app_screenshot(
        env={"PORT": "43000", "RAILS_ENV": "test"}, workspace=tmp_path,
        out_path=str(tmp_path / "s.png"),
        port_check=check, runner=_writing_runner(tmp_path),
        spawner=spawner, sleep=lambda s: None, boot_timeout=30,
    )
    assert out == str(tmp_path / "s.png")
    assert spawn_args["cmd"][:2] == ["bin/rails", "server"]
    assert spawn_args["cwd"] == str(tmp_path)
    assert terminated["n"] == 1  # server we launched is torn down after capture


def test_boot_returns_none_when_no_command_inferred(tmp_path: Path) -> None:
    spawned = {"n": 0}
    out = boot_and_capture_app_screenshot(
        env={"PORT": "43000"}, workspace=tmp_path, out_path=str(tmp_path / "s.png"),
        port_check=lambda h, p: False,  # not serving
        runner=_writing_runner(tmp_path),
        spawner=lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1),
        sleep=lambda s: None,
    )
    assert out is None
    assert spawned["n"] == 0  # unknown project → don't guess a command


def test_boot_returns_none_and_kills_when_server_never_comes_up(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "rails").write_text("#!/usr/bin/env ruby\n")
    terminated = {"n": 0}
    class _Proc:
        def terminate(self): terminated["n"] += 1
        def wait(self, timeout=None): return 0
        def poll(self): return None
    out = boot_and_capture_app_screenshot(
        env={"PORT": "43000"}, workspace=tmp_path, out_path=str(tmp_path / "s.png"),
        port_check=lambda h, p: False,  # never comes up
        runner=_writing_runner(tmp_path),
        spawner=lambda cmd, **kw: _Proc(), sleep=lambda s: None, boot_timeout=3,
    )
    assert out is None
    assert terminated["n"] == 1  # must not leak the launched process


def test_terminate_escalates_to_kill_when_sigterm_ignored() -> None:
    """A stubborn server that ignores SIGTERM (wait() times out) MUST be killed —
    otherwise boot_and_capture leaks a live app server on every QA run."""
    import subprocess as _sp

    from performer.qa_capture import _terminate

    calls = {"terminate": 0, "kill": 0, "waits": 0}

    class _Stubborn:
        def terminate(self):
            calls["terminate"] += 1

        def kill(self):
            calls["kill"] += 1

        def wait(self, timeout=None):
            calls["waits"] += 1
            # First wait (post-SIGTERM) times out; second wait (post-SIGKILL) reaps.
            if calls["kill"] == 0:
                raise _sp.TimeoutExpired(cmd="app", timeout=timeout or 0)
            return -9

    _terminate(_Stubborn())
    assert calls["terminate"] == 1
    assert calls["kill"] == 1  # escalated to SIGKILL after the graceful wait timed out
    assert calls["waits"] >= 2  # reaped the SIGKILL so no zombie
