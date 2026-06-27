#!/usr/bin/env python3
"""Atomic per-backend QA smoke test (077).

Exercises EACH backend's ability to do the work of a QA stage — launch the CLI,
reach the model, and return a parseable QA verdict — WITHOUT running coordinare's
board / lifecycle / env-bootstrap. Each backend runs in its own ephemeral
performer container (the same image coordinare uses), in parallel, and we POST a
single QA job straight to the container's HTTP API and read the result.

This turns the serial, hours-long "discover one per-backend failure per
lifecycle stage" loop into one ~few-minute parallel run. It would have caught,
up front:
  - openclaw  → CLI crash at launch (Node 22 vs project Node 18)
  - opencode  → empty .output (adapter never set BackendStatus.output)
  - junie/openclaw → prose-not-JSON (BACKEND_FORMAT_ERROR)

Usage:
  scripts/smoke_backends.py [--backends id1,id2] [--keep] [--repo URL] [--pr URL]

Reads performer endpoints + their env from config.yaml, expands ${VAR} from .env.
Requires: docker, the coordinare-performer:full image, .env with the proxy keys.
Run from repo root.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
IMAGE = "coordinare-performer:full"
JOB_TIMEOUT_S = 420  # per-backend wall clock for the QA job
READY_TIMEOUT_S = 240  # container HTTP readiness (CLI install can be slow)

# Representative QA persona: ask for the JSON verdict contract the qa role parses.
QA_PERSONA = (
    "You are the QA reviewer. Inspect the repository and the change under review, "
    "then return ONLY a single JSON object with this exact shape and nothing else:\n"
    '{"criteria_checked": <int>, "criteria_passed": <int>, '
    '"failures": [{"criterion": "...", "expected": "...", "actual": "..."}], '
    '"verification_steps": ["..."], "environment_error": ""}\n'
    "If you cannot run tests or capture screenshots in this environment, set "
    'environment_error to a short note and return failures: []. Do NOT output prose '
    "outside the JSON object."
)


def _load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


_VAR = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand(value: str, dotenv: dict[str, str]) -> str:
    return _VAR.sub(lambda m: dotenv.get(m.group(1), os.environ.get(m.group(1), "")), value)


def _gh_token(dotenv: dict[str, str]) -> str:
    """Resolve a GitHub token for repo clone + the job's required secret.

    .env's GH_TOKEN/GITHUB_TOKEN are often empty here (coordinare mints App
    installation tokens at runtime); the usable PAT lives in ../.gh_token
    (the workspace convention). Prefer a non-empty .env value, else that file.
    """
    for k in ("GH_TOKEN", "GITHUB_TOKEN"):
        v = (dotenv.get(k) or os.environ.get(k) or "").strip()
        if v:
            return v
    f = REPO_ROOT.parent / ".gh_token"
    if f.is_file():
        return f.read_text().strip()
    return ""


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _resolve_mode_model(cfg: dict, rc: dict) -> str:
    """Resolve a performer's model via either a direct `model:` or the
    mode -> model_endpoints chain (mode.tool -> model_endpoints[name].model)."""
    if rc.get("model"):
        return str(rc["model"])
    mode_name = rc.get("mode")
    if not mode_name:
        return ""
    modes = {m.get("name"): m for m in (cfg.get("modes") or [])}
    tool = (modes.get(mode_name) or {}).get("tool")
    if not tool:
        return ""
    mes = {e.get("name"): e for e in (cfg.get("model_endpoints") or [])}
    return str((mes.get(tool) or {}).get("model", ""))


def _backend_model(cfg: dict, endpoint: dict) -> str:
    """Pick the model this endpoint's first role uses (from performers:)."""
    performers = cfg.get("performers", {}) or {}
    for role in endpoint.get("roles", []):
        rc = performers.get(role)
        if isinstance(rc, dict):
            m = _resolve_mode_model(cfg, rc)
            if m:
                return m
    default = performers.get("default", {}) or {}
    return _resolve_mode_model(cfg, default) or str(default.get("model", ""))


# --- spec 122 US2: compatibility-matrix row building (pure, unit-tested) ---

_GATEWAY_UNAVAIL_MARKERS = (
    "no healthy deployments",
    "no deployments available",
    "connection refused",
    "connecterror",
    "read timed out",
    "readtimeout",
    "502 bad gateway",
    "503 service",
    "504 gateway",
)


def gateway_unavailable(text: str) -> bool:
    """True when a failure looks like a gateway-availability problem (model not
    served / proxy 5xx / connection) rather than a backend incompatibility."""
    t = (text or "").lower()
    return any(m in t for m in _GATEWAY_UNAVAIL_MARKERS)


def compute_verdict(*, launched: bool, completed: bool, contract_satisfied: bool,
                    gateway_available: bool) -> str:
    """Map per-backend signals to the contract verdict (contracts/compatibility-matrix.md)."""
    if not gateway_available:
        return "gateway_unavailable"
    if launched and completed and contract_satisfied:
        return "compatible"
    return "incompatible"


# A valid QA terminal status proves the backend reached the model and emitted its
# role contract — the env-block variant (qa_env_blocked) is still backend↔gateway
# compatible (the env limit is a task concern, not a wire/model concern).
_VALID_BACKEND_STATUSES = frozenset({"qa_passed", "qa_failed", "qa_env_blocked"})


def build_matrix_row(result: dict) -> dict:
    """Map a run_one() result to the spec-122 compatibility-matrix contract row.

    Compatibility keys on the performer's terminal STATUS, not the smoke job
    state: a backend that emits a valid QA verdict (incl. qa_env_blocked) reached
    the model through the gateway and produced its role contract.
    """
    launched = bool(result.get("launched"))
    performer_status = result.get("performer_status") or ""
    completed = performer_status in _VALID_BACKEND_STATUSES
    output_present = int(result.get("output_len") or 0) > 0
    contract_satisfied = completed  # a valid backend status IS the role contract
    fail_text = f"{result.get('error', '')} {result.get('detail', '')}"
    gateway_available = not (not completed and gateway_unavailable(fail_text))
    # On the LiteLLM path no orchestrator normalizers are applied; a pass ⇒ none
    # needed. A raw failure is investigated per-backend in US1.
    normalizers_needed: list[str] = []
    return {
        "backend": result.get("backend", ""),
        "endpoint_id": result.get("endpoint", ""),
        "model": result.get("model", ""),
        "launched": launched,
        "completed": completed,
        "output_present": output_present,
        "contract_satisfied": contract_satisfied,
        "normalizers_needed": normalizers_needed,
        "gateway_available": gateway_available,
        "verdict": compute_verdict(launched=launched, completed=completed,
                                   contract_satisfied=contract_satisfied,
                                   gateway_available=gateway_available),
        "note": (result.get("error") or result.get("detail") or "")[:80],
    }


def run_one(endpoint: dict, cfg: dict, dotenv: dict, repo_url: str, pr_url: str) -> dict:
    eid = endpoint["id"]
    backend = (endpoint.get("env", {}) or {}).get("BACKEND", "")
    model = _backend_model(cfg, endpoint)
    result = {"endpoint": eid, "backend": backend, "model": model,
              "launched": False, "state": None, "output_len": 0,
              "verdict_parseable": False, "error": "", "detail": ""}
    port = _free_port()
    name = f"smoke-{eid}-{uuid.uuid4().hex[:6]}"
    # Container env: the endpoint's declared env, with ${VAR} expanded.
    cenv = {k: _expand(str(v), dotenv) for k, v in (endpoint.get("env", {}) or {}).items()}
    gh_token = _gh_token(dotenv)
    docker_env = []
    for k, v in cenv.items():
        docker_env += ["-e", f"{k}={v}"]
    docker_env += ["-e", f"GITHUB_TOKEN={gh_token}"]
    # The performer's secret sources are opt-in (default disabled) — enable
    # init_payload (job secrets) + env so the required GITHUB_TOKEN resolves;
    # otherwise POST /jobs returns 422 secret_missing.
    docker_env += [
        "-e", "PERFORMER_SECRET_SOURCE_INIT_PAYLOAD=1",
        "-e", "PERFORMER_SECRET_SOURCE_ENV=1",
    ]
    # Backend-specific API-key pre-flight (job_runner): codex needs OPENAI_API_KEY,
    # claude_code needs ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN. Coordinare name-maps
    # the endpoint's proxy token into these; mirror that with the LiteLLM master key
    # (the proxy owns the upstream credential). Other backends need no API key here.
    backend_key_map = {"codex": "OPENAI_API_KEY", "claude_code": "ANTHROPIC_AUTH_TOKEN"}
    extra_secrets: dict[str, str] = {}
    _key_name = backend_key_map.get(backend.replace("-", "_"))
    if _key_name:
        master = (dotenv.get("LITELLM_MASTER_KEY") or os.environ.get("LITELLM_MASTER_KEY") or "").strip()
        if master:
            extra_secrets[_key_name] = master
            docker_env += ["-e", f"{_key_name}={master}"]
    image = endpoint.get("image", IMAGE)
    # 122: mount routing.yaml so the observe/normalize/translate shim path is
    # actually exercised (the endpoint's SELFHOSTED_ROUTING_CONFIG points at it;
    # without the file the launch fails fast). Mirrors the live volume mount.
    docker_mounts: list[str] = []
    for vol in (endpoint.get("volumes", []) or []):
        hp, cp = vol.get("host_path"), vol.get("container_path")
        if hp and cp:
            docker_mounts += ["-v", f"{hp}:{cp}:{vol.get('mode', 'ro')}"]
    cid = None
    try:
        run = subprocess.run(
            ["docker", "run", "-d", "--name", name, "-p", f"{port}:8088",
             *docker_mounts, *docker_env, image],
            capture_output=True, text=True, timeout=60,
        )
        if run.returncode != 0:
            result["error"] = f"docker run failed: {run.stderr.strip()[:200]}"
            return result
        cid = run.stdout.strip()
        base = f"http://127.0.0.1:{port}"
        # Wait for readiness (CLI install happens at container start).
        deadline = time.time() + READY_TIMEOUT_S
        ready = False
        while time.time() < deadline:
            try:
                r = httpx.get(f"{base}/healthz", timeout=5)
                if r.status_code == 200:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(3)
        if not ready:
            result["error"] = "container never became ready (CLI install/crash?)"
            return result
        result["launched"] = True
        # POST the QA job.
        job_id = uuid.uuid4().hex
        payload = {
            "job_id": job_id,
            "card_id": f"smoke-qa-{backend}",
            "role": "qa",
            "backend": backend,
            "persona": QA_PERSONA,
            "repo_url": repo_url,
            # NOT "main": cloning then fetching origin main:main into a repo
            # already on main → "refusing to fetch into current branch" (exit 128).
            # A synthetic branch makes clone fall back to checkout -b from default.
            "branch": f"smoke-qa-{uuid.uuid4().hex[:8]}",
            "secrets": {"GITHUB_TOKEN": gh_token, **extra_secrets},
            "metadata": {
                "title": "Smoke QA: does this backend produce a QA verdict?",
                "model": model,
                "pr_url": pr_url,
                "acceptance_criteria": ["The change under review is sound."],
            },
        }
        pr = httpx.post(f"{base}/jobs", json=payload, timeout=15)
        if pr.status_code != 202:
            result["error"] = f"POST /jobs -> {pr.status_code}: {pr.text[:150]}"
            return result
        # Poll to terminal.
        deadline = time.time() + JOB_TIMEOUT_S
        status = {}
        while time.time() < deadline:
            try:
                jr = httpx.get(f"{base}/jobs/{job_id}", timeout=10)
                if jr.status_code == 200:
                    status = jr.json()
                    st = status.get("state")
                    if st in ("succeeded", "failed", "cancelled"):
                        break
            except Exception:
                pass
            time.sleep(5)
        result["state"] = status.get("state")
        res = status.get("result") or {}
        summary = res.get("summary") or ""
        out = summary + (res.get("logs_excerpt") or "")
        result["output_len"] = len(out)
        # Prefer the human-readable summary (carries the real error) over the code.
        result["detail"] = (summary or res.get("error_code") or status.get("reason") or "")[:180]
        # spec 122: the performer's terminal status is the compatibility signal —
        # a valid QA terminal (qa_passed/qa_failed/qa_env_blocked) proves the
        # backend reached the model + emitted its role contract; error /
        # BACKEND_FORMAT_ERROR means it could not use the model.
        result["performer_status"] = ""
        try:
            _obj = json.loads(summary)
            if isinstance(_obj, dict):
                result["performer_status"] = str(_obj.get("status") or "")
        except Exception:
            pass
        # Dump the full result for post-mortem (the matrix only shows a snippet).
        try:
            d = REPO_ROOT / "tmp" / "smoke_results"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{eid}.json").write_text(json.dumps(status, indent=2, default=str))
        except OSError:
            pass
        # Verdict parseable? look for a JSON object with qa-ish keys in the output.
        m = re.search(r"\{.*\}", out, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
                result["verdict_parseable"] = isinstance(obj, dict) and (
                    "criteria_checked" in obj or "failures" in obj
                )
            except Exception:
                pass
        return result
    except subprocess.TimeoutExpired:
        result["error"] = "docker run timed out"
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return result
    finally:
        if cid:
            # 122: capture container logs (proxy launch markers: observe_launched,
            # upstream host) for the observe-cutover post-mortem before removal.
            try:
                lg = subprocess.run(["docker", "logs", name], capture_output=True,
                                    text=True, timeout=30)
                d = REPO_ROOT / "tmp" / "smoke_results"
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{eid}.containerlog").write_text((lg.stdout or "") + (lg.stderr or ""))
            except Exception:
                pass
        if cid and not result.get("_keep"):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backends", help="comma-separated endpoint ids; default all")
    ap.add_argument("--repo", default="https://github.com/ViviDynamics/website.git")
    ap.add_argument("--pr", default="https://github.com/ViviDynamics/website/pull/154")
    ap.add_argument("--config", default=str(REPO_ROOT / "config.yaml"))
    ap.add_argument("--via-litellm", action="store_true",
                    help="use the bundled LiteLLM test config (specs/122-litellm-backend-routing/config.litellm-test.yaml)")
    ap.add_argument("--matrix", action="store_true",
                    help="emit the spec-122 compatibility-matrix rows + tmp/litellm_matrix.json")
    args = ap.parse_args()
    if args.via_litellm:
        args.config = str(REPO_ROOT / "specs/122-litellm-backend-routing/config.litellm-test.yaml")

    cfg = yaml.safe_load(Path(args.config).read_text())
    dotenv = _load_env(REPO_ROOT / ".env")
    endpoints = cfg.get("performer_endpoints", []) or []
    if args.backends:
        wanted = set(args.backends.split(","))
        endpoints = [e for e in endpoints if e["id"] in wanted]
    if not endpoints:
        print("no matching endpoints", file=sys.stderr)
        return 2

    # The Spark (Ollama) serves ~one request at a time — running backends in
    # parallel creates contention that looks like backend failures but isn't. So
    # run SEQUENTIALLY, grouped by model to minimise Ollama model-reload thrash
    # between consecutive backends.
    endpoints.sort(key=lambda e: _backend_model(cfg, e))
    print(f"Smoke-testing {len(endpoints)} backend(s) SEQUENTIALLY "
          f"(Spark is single-request; repo={args.repo}, pr={args.pr})\n")
    results = []
    for e in endpoints:
        print(f"  -> {e['id']} (model={_backend_model(cfg, e) or '?'}) ...", flush=True)
        r = run_one(e, cfg, dotenv, args.repo, args.pr)
        results.append(r)
        print(f"     launch={r['launched']} state={r['state']} "
              f"verdict={'parse' if r['verdict_parseable'] else '-'} {r['error'] or r['detail']}"[:120],
              flush=True)

    results.sort(key=lambda r: r["endpoint"])
    print(f"{'ENDPOINT':<22}{'BACKEND':<12}{'LAUNCH':<8}{'STATE':<11}{'OUT':<7}{'VERDICT':<9}NOTE")
    print("-" * 100)
    ok = 0
    for r in results:
        launch = "yes" if r["launched"] else "NO"
        verdict = "parse" if r["verdict_parseable"] else "-"
        note = r["error"] or r["detail"] or ""
        if r["launched"] and r["state"] == "succeeded" and r["verdict_parseable"]:
            ok += 1
        print(f"{r['endpoint']:<22}{r['backend']:<12}{launch:<8}"
              f"{r['state']!s:<11}{r['output_len']:<7}{verdict:<9}{note[:50]}")
    print("-" * 100)
    print(f"{ok}/{len(results)} backends produced a parseable QA verdict")

    if args.matrix:
        rows = [build_matrix_row(r) for r in results]
        outdir = REPO_ROOT / "tmp"
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "litellm_matrix.json").write_text(json.dumps(rows, indent=2))
        print("\n=== spec-122 compatibility matrix ===")
        print(f"{'BACKEND':<14}{'MODEL':<28}{'VERDICT':<20}NORMALIZERS_NEEDED")
        print("-" * 90)
        for row in rows:
            print(f"{row['backend']:<14}{row['model'][:27]:<28}{row['verdict']:<20}"
                  f"{row['normalizers_needed']}")
        print("\nartifact: tmp/litellm_matrix.json")

    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
