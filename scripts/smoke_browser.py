#!/usr/bin/env python3
"""Atomic per-backend BROWSER-CONTROL smoke test (077).

Challenges each backend to actually DRIVE Chrome: "take a screenshot of
https://example.com and save it to /tmp/agent_shot.png using the tools at
your disposal." Confirms two things per backend:
  1. the agent can write + run the code to drive a headless browser, and
  2. Chrome is present + accessible in the performer image.

The coordinare-performer:full image ships Playwright + Chromium
(`playwright install --with-deps chromium`); the binary lives under the
Playwright cache (not on PATH as `chromium`). A direct baseline screenshot from
the image succeeds (~1.6 MB PNG), so any failure here is the *agent's* inability
to find/drive the browser, not a missing browser.

Runs SEQUENTIALLY (the model host serves ~one request at a time). After each job we
docker-exec the container to confirm the screenshot file exists + is a real PNG,
then docker-cp it out to tmp/smoke_shots/<endpoint>.png as proof.

Usage: scripts/smoke_browser.py [--backends id1,id2]   (run from repo root)
"""
from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

# Reuse the validated QA-smoke harness helpers.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import yaml
from smoke_backends import (
    IMAGE,
    READY_TIMEOUT_S,
    REPO_ROOT,
    _backend_model,
    _expand,
    _free_port,
    _gh_token,
    _load_env,
)

SHOT_PATH = "/tmp/agent_shot.png"
JOB_TIMEOUT_S = 480

SHOT_PERSONA = (
    "Your ONLY task: take a screenshot of the homepage at https://example.com "
    f"and save it to the file {SHOT_PATH}.\n"
    "Playwright (Python) with Chromium IS installed in this environment — the "
    "Chromium binary lives under the Playwright browser cache (it is NOT on PATH as "
    "`chromium`/`google-chrome`), so drive it via Playwright. Write and RUN the code "
    "needed, e.g. a short Python script:\n"
    "  from playwright.sync_api import sync_playwright\n"
    "  with sync_playwright() as p:\n"
    "      b = p.chromium.launch()\n"
    "      pg = b.new_page(); pg.goto('https://example.com', wait_until='domcontentloaded')\n"
    f"      pg.screenshot(path='{SHOT_PATH}', full_page=True); b.close()\n"
    "If Playwright's python package is missing, `pip install playwright` (browsers "
    "are already downloaded). Do NOT use any third-party screenshot or "
    "URL-rendering service (Thum.io, screenshotapi, urlbox, microlink, "
    "htmlcsstoimage, etc.): they capture an external or cached page, not a real "
    "in-environment render, and count as fabricated evidence. Drive the local "
    "Playwright + Chromium install only. Do NOT modify the repo, commit, or open a "
    f"PR. You are done only when {SHOT_PATH} exists and is a valid non-empty PNG."
)

backend_key_map = {"codex": "OPENAI_API_KEY", "claude_code": "ANTHROPIC_AUTH_TOKEN"}


def run_one(endpoint: dict, cfg: dict, dotenv: dict, repo_url: str) -> dict:
    eid = endpoint["id"]
    backend = (endpoint.get("env", {}) or {}).get("BACKEND", "")
    model = _backend_model(cfg, endpoint)
    result = {"endpoint": eid, "backend": backend, "model": model, "launched": False,
              "state": None, "shot_bytes": 0, "valid_png": False, "summary": "", "error": ""}
    port = _free_port()
    name = f"shot-{eid}-{uuid.uuid4().hex[:6]}"
    cenv = {k: _expand(str(v), dotenv) for k, v in (endpoint.get("env", {}) or {}).items()}
    gh_token = _gh_token(dotenv)
    docker_env = []
    for k, v in cenv.items():
        docker_env += ["-e", f"{k}={v}"]
    docker_env += ["-e", f"GITHUB_TOKEN={gh_token}",
                   "-e", "PERFORMER_SECRET_SOURCE_INIT_PAYLOAD=1",
                   "-e", "PERFORMER_SECRET_SOURCE_ENV=1"]
    # Mount the endpoint's declared volumes (e.g. the SELFHOSTED_ROUTING_CONFIG
    # routing.yaml) so dispatch sees exactly what the daemon mounts — otherwise
    # an endpoint that points SELFHOSTED_ROUTING_CONFIG at a mounted file fails
    # dispatch with FileNotFoundError before the agent ever runs.
    docker_vols: list[str] = []
    for vol in (endpoint.get("volumes", []) or []):
        host = _expand(str(vol.get("host_path", "")), dotenv)
        cont = str(vol.get("container_path", ""))
        mode = str(vol.get("mode", "ro"))
        if host and cont and Path(host).exists():
            docker_vols += ["-v", f"{host}:{cont}:{mode}"]
    extra_secrets: dict[str, str] = {}
    key_name = backend_key_map.get(backend.replace("-", "_"))
    if key_name:
        master = (dotenv.get("LITELLM_MASTER_KEY") or "").strip()
        if master:
            extra_secrets[key_name] = master
            docker_env += ["-e", f"{key_name}={master}"]
    image = endpoint.get("image", IMAGE)
    cid = None
    try:
        run = subprocess.run(
            ["docker", "run", "-d", "--name", name, "-p", f"{port}:8088", *docker_env, *docker_vols, image],
            capture_output=True, text=True, timeout=60,
        )
        if run.returncode != 0:
            result["error"] = f"docker run failed: {run.stderr.strip()[:150]}"
            return result
        cid = run.stdout.strip()
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + READY_TIMEOUT_S
        while time.time() < deadline:
            try:
                if httpx.get(f"{base}/healthz", timeout=5).status_code == 200:
                    result["launched"] = True
                    break
            except Exception:
                pass
            time.sleep(3)
        if not result["launched"]:
            result["error"] = "container never became ready"
            return result
        job_id = uuid.uuid4().hex
        payload = {
            # role="diagnostic" (077): free-form viability probe — runs the
            # agent agentically with full tooling and NO lifecycle scaffolding
            # (no PR, no JSON verdict, no verify/inference). The other roles all
            # distort a free-form task: implementing 422s on the zero-commit
            # write-to-/tmp task, qa rubber-stamps a verdict without executing,
            # env_bootstrap runs the install framing. Ground truth: the PNG.
            "job_id": job_id, "card_id": f"shot-{backend}", "role": "diagnostic",
            "backend": backend, "persona": SHOT_PERSONA, "repo_url": repo_url,
            "branch": f"shot-{uuid.uuid4().hex[:8]}",
            "secrets": {"GITHUB_TOKEN": gh_token, **extra_secrets},
            "metadata": {"title": "Screenshot vividynamics.com homepage", "model": model,
                         "acceptance_criteria": [f"A PNG screenshot exists at {SHOT_PATH}"]},
        }
        pr = httpx.post(f"{base}/jobs", json=payload, timeout=15)
        if pr.status_code != 202:
            result["error"] = f"POST /jobs -> {pr.status_code}: {pr.text[:120]}"
            return result
        deadline = time.time() + JOB_TIMEOUT_S
        status = {}
        while time.time() < deadline:
            try:
                jr = httpx.get(f"{base}/jobs/{job_id}", timeout=10)
                if jr.status_code == 200:
                    status = jr.json()
                    if status.get("state") in ("succeeded", "failed", "cancelled"):
                        break
            except Exception:
                pass
            time.sleep(5)
        result["state"] = status.get("state")
        # Capture the full job result (summary / logs_excerpt / error_code) for diagnosis.
        outdir = REPO_ROOT / "tmp" / "smoke_shots"
        outdir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(Exception):
            (outdir / f"{eid}.json").write_text(json.dumps(status, indent=2, default=str))
        res = status.get("result") or {}
        result["summary"] = (res.get("summary") or "")[:200]
        result["error"] = result["error"] or (res.get("error") or res.get("error_code") or "")
        # Find the screenshot ANYWHERE (agent may have saved it under a different path).
        find = subprocess.run(
            ["docker", "exec", cid, "sh", "-c",
             f"stat -c %s {SHOT_PATH} 2>/dev/null || "
             "find /tmp /root /workspace -name '*.png' -size +5k 2>/dev/null "
             "-printf '%s %p\\n' | sort -rn | head -1 | cut -d' ' -f1"],
            capture_output=True, text=True, timeout=25,
        )
        first = (find.stdout.strip().split() or ["0"])[0]
        try:
            result["shot_bytes"] = int(first or "0")
        except ValueError:
            result["shot_bytes"] = 0
        # Ground truth: the PNG either exists + is a valid image or it doesn't.
        # (The old `wrote_code` grep false-matched the persona text on disk, so
        # it's dropped — a real, non-empty PNG proves the agent drove Chrome.)
        if result["shot_bytes"] > 5000:
            magic = subprocess.run(
                ["docker", "exec", cid, "sh", "-c",
                 f"head -c8 {SHOT_PATH} 2>/dev/null | od -An -tx1 | tr -d ' \\n'"],
                capture_output=True, text=True, timeout=20,
            )
            result["valid_png"] = magic.stdout.strip().startswith("89504e47")
        # Copy a real screenshot out as proof.
        if result["shot_bytes"] > 5000:
            cp = subprocess.run(
                ["docker", "exec", cid, "sh", "-c",
                 f"test -f {SHOT_PATH} && echo {SHOT_PATH} || "
                 "find /tmp /root /workspace -name '*.png' -size +5k 2>/dev/null | head -1"],
                capture_output=True, text=True, timeout=20,
            )
            src = cp.stdout.strip()
            if src:
                subprocess.run(["docker", "cp", f"{cid}:{src}", str(outdir / f"{eid}.png")],
                               capture_output=True, timeout=30)
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:160]
        return result
    finally:
        if cid:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backends")
    ap.add_argument("--repo", default="https://github.com/ViviDynamics/website.git")
    ap.add_argument("--config", default=str(REPO_ROOT / "config.yaml"))
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text())
    dotenv = _load_env(REPO_ROOT / ".env")
    endpoints = cfg.get("performer_endpoints", []) or []
    if args.backends:
        wanted = set(args.backends.split(","))
        endpoints = [e for e in endpoints if e["id"] in wanted]
    endpoints.sort(key=lambda e: _backend_model(cfg, e))
    print(f"Browser-control smoke: {len(endpoints)} backend(s) SEQUENTIALLY "
          f"(screenshot https://example.com -> {SHOT_PATH})\n")
    results = []
    for e in endpoints:
        print(f"  -> {e['id']} (model={_backend_model(cfg, e) or '?'}) ...", flush=True)
        r = run_one(e, cfg, dotenv, args.repo)
        results.append(r)
        print(f"     launch={r['launched']} state={r['state']} shot_bytes={r['shot_bytes']} "
              f"valid_png={r['valid_png']} {r['error']}"[:120], flush=True)
    results.sort(key=lambda r: r["endpoint"])
    print(f"\n{'ENDPOINT':<22}{'BACKEND':<12}{'LAUNCH':<8}{'STATE':<11}{'SHOT_KB':<9}{'PNG':<5}NOTE")
    print("-" * 100)
    ok = 0
    for r in results:
        shot_ok = r["shot_bytes"] > 5000 and r["valid_png"]
        if shot_ok:
            ok += 1
        print(f"{r['endpoint']:<22}{r['backend']:<12}{'yes' if r['launched'] else 'NO':<8}"
              f"{r['state']!s:<11}{r['shot_bytes'] // 1024:<9}"
              f"{'yes' if r['valid_png'] else '-':<5}{(r['error'] or r['summary'])[:45]}")
    print("-" * 100)
    print(f"{ok}/{len(results)} backends produced a real screenshot "
          f"(proofs in tmp/smoke_shots/)")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
