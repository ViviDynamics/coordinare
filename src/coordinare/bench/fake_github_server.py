"""Spec 151 — the performer-facing fake GitHub boundary.

A real performer talks to GitHub over the network *itself*: it clones/pushes a
git remote and opens its PR + polls check-runs via REST. To keep GitHub fully
faked for a real-model run, this module stands up two harness-local listeners on
loopback, both delegating to the **same** ``FakeGitHubService`` (one PR identity,
one real-pytest CI — no local state of its own):

* a ``git daemon`` serving the run's bare repo (clone via upload-pack, push via
  receive-pack) over ``git://127.0.0.1:<git_port>``; and
* an aiohttp REST surface answering the five endpoints the performer's
  ``agent/performer/src/performer/github.py`` calls, plus a best-effort
  ``POST /graphql`` — in the exact shapes that file parses
  (``specs/151-real-performers/contracts/performer-facing-rest.md``).

Lifecycle is owned by the runner and teardown-safe (``stop()`` never raises).
The performer container reaches these over the Docker bridge via
``host.docker.internal`` (added with ``--add-host=host.docker.internal:host-gateway``,
portable across Docker Desktop and native Linux). So the servers bind ``0.0.0.0``
and advertise two views: a HOST-facing ``127.0.0.1`` address (the coordinare's own
clone/rebase + the readiness probe) and a CONTAINER-facing ``host.docker.internal``
address handed to the performer — which accepts the latter's ``http`` GitHub URL
only under the ``ALLOW_HOST_GATEWAY_GITHUB`` bench opt-in.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from aiohttp import web

    from coordinare.services.fake_github import FakeGitHubService

logger = structlog.get_logger(__name__)

_DEFAULT_GIT_PORT = 9418


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
        return True


def _free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


class FakeGitHubServer:
    """Loopback git + REST boundary backed by an injected ``FakeGitHubService``.

    Holds no PR/CI state — every read/write delegates to ``fake`` so the
    performer-side and coordinare-side views can never diverge (FR-005).
    """

    def __init__(
        self,
        fake: FakeGitHubService,
        *,
        bare_repo: str | Path,
        head_ref_index: dict[str, str],
        scratch: str | Path,
        org: str = "bench-org",
        project: str = "bench-repo",
        host: str = "127.0.0.1",
        git_port: int = _DEFAULT_GIT_PORT,
        bind_host: str | None = None,
        gateway_host: str = "host.docker.internal",
    ) -> None:
        self._fake = fake
        self._bare_repo = Path(bare_repo).resolve()
        # head branch → seeded card id, so POST /pulls resolves the pushed branch
        # back to the card open_pr() needs (D3). Mutable so the runner can seed it.
        self.head_ref_index = dict(head_ref_index)
        self._scratch = Path(scratch)
        self._org = org
        self._project = project
        # _host is the HOST-facing advertised address (coordinare + probe reach it);
        # _bind_host is what the listeners actually bind (0.0.0.0 for the real bench
        # so a bridged container reaching in over host-gateway is accepted);
        # _gateway_host is the CONTAINER-facing name (host.docker.internal).
        # ponytail: 0.0.0.0 exposes the fakes on the host LAN for the run's ~10 min —
        # a stateless, secret-free test fake. Bind to the bridge-gateway IP if that
        # exposure ever matters.
        self._host = host
        self._bind_host = bind_host or host
        self._gateway_host = gateway_host
        self._git_port = git_port

        self._git_proc: asyncio.subprocess.Process | None = None
        self._runner: web.AppRunner | None = None
        self._rest_port: int = 0

    # ---- resolved addresses (valid after start) --------------------------

    @property
    def git_port(self) -> int:
        return self._git_port

    @property
    def git_base_url(self) -> str:
        """HOST-facing ``config.git_base_url`` (coordinare clone + rebase reach this)."""
        return f"git://{self._host}:{self._git_port}"

    @property
    def performer_git_base_url(self) -> str:
        """CONTAINER-facing git base (``config.performer_git_base_url``)."""
        return f"git://{self._gateway_host}:{self._git_port}"

    @property
    def rest_base_url(self) -> str:
        """HOST-facing REST base (probe/tests reach this)."""
        return f"http://{self._host}:{self._rest_port}"

    @property
    def graphql_url(self) -> str:
        return f"{self.rest_base_url}/graphql"

    @property
    def performer_rest_base_url(self) -> str:
        """CONTAINER-facing REST base (``config.github_api_url``)."""
        return f"http://{self._gateway_host}:{self._rest_port}"

    @property
    def performer_graphql_url(self) -> str:
        """CONTAINER-facing GraphQL endpoint (``config.github_graphql_url``)."""
        return f"{self.performer_rest_base_url}/graphql"

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Start the git daemon + REST server and probe reachability (fail fast)."""
        await self._start_git_daemon()
        await self._start_rest_server()
        await self._probe()
        logger.info(
            "fake_github_server.started",
            git=self.git_base_url,
            rest=self.rest_base_url,
        )

    async def _start_git_daemon(self) -> None:
        # Lay out a base-path the daemon exports; the run's bare repo is symlinked
        # in at org/project.git so git://host/org/project.git resolves to it.
        git_root = self._scratch / "git-root"
        (git_root / self._org).mkdir(parents=True, exist_ok=True)
        link = git_root / self._org / f"{self._project}.git"
        with contextlib.suppress(FileExistsError):
            os.symlink(self._bare_repo, link)

        if not _port_free(self._git_port):
            self._git_port = _free_port(self._host)

        self._git_proc = await asyncio.create_subprocess_exec(
            "git", "daemon",
            "--reuseaddr",
            f"--listen={self._bind_host}",
            f"--port={self._git_port}",
            f"--base-path={git_root}",
            "--export-all",
            "--enable=upload-pack",
            "--enable=receive-pack",
            str(git_root),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )

    def _build_app(self) -> web.Application:
        """The REST routes (exposed so tests can drive them without the daemon)."""
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/repos/{owner}/{repo}", self._h_repo)
        app.router.add_post("/repos/{owner}/{repo}/pulls", self._h_create_pr)
        app.router.add_get("/repos/{owner}/{repo}/pulls", self._h_list_pulls)
        app.router.add_get("/repos/{owner}/{repo}/pulls/{number}", self._h_get_pull)
        app.router.add_patch("/repos/{owner}/{repo}/pulls/{number}", self._h_patch_pull)
        app.router.add_post(
            "/repos/{owner}/{repo}/pulls/{number}/reviews", self._h_create_review,
        )
        app.router.add_get(
            "/repos/{owner}/{repo}/issues/{number}/comments", self._h_issue_comments,
        )
        app.router.add_post(
            "/repos/{owner}/{repo}/issues/{number}/comments", self._h_post_issue_comment,
        )
        app.router.add_get(
            "/repos/{owner}/{repo}/commits/{ref}/check-runs", self._h_check_runs,
        )
        app.router.add_post("/graphql", self._h_graphql)
        return app

    async def _start_rest_server(self) -> None:
        from aiohttp import web

        self._runner = web.AppRunner(self._build_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._bind_host, 0)
        await site.start()
        self._rest_port = int(self._runner.addresses[0][1])

    async def _probe(self) -> None:
        """TCP-connect to both listeners; raise an actionable error, never hang."""
        for label, port in (("git", self._git_port), ("rest", self._rest_port)):
            for _ in range(50):  # ~5s
                try:
                    _reader, writer = await asyncio.open_connection(self._host, port)
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            else:
                raise RuntimeError(
                    f"fake GitHub {label} server never bound {self._host}:{port} "
                    "— cannot dispatch performers (check the port is free / git installed)",
                )

    async def stop(self) -> None:
        """Kill the daemon + close the server. Best-effort; never raises."""
        if self._git_proc is not None:
            with contextlib.suppress(ProcessLookupError):
                if os.name == "posix":
                    os.killpg(self._git_proc.pid, signal.SIGKILL)
                else:
                    self._git_proc.kill()
            with contextlib.suppress(Exception):
                await self._git_proc.wait()
            self._git_proc = None
        if self._runner is not None:
            with contextlib.suppress(Exception):
                await self._runner.cleanup()
            self._runner = None

    # ---- REST handlers (delegate everything to the FakeGitHubService) -----

    async def _h_repo(self, _request: web.Request) -> web.Response:
        from aiohttp import web
        # Fixtures are materialized with a `main` default branch (fixtures.py).
        return web.json_response({"default_branch": "main"})

    async def _h_create_pr(self, request: web.Request) -> web.Response:
        from aiohttp import web
        try:
            body = await request.json()
        except Exception:
            body = {}
        head = str(body.get("head") or "")
        base = str(body.get("base") or "main")
        try:
            existing = self._pr_for_head(head)
            if existing is not None:
                # Idempotent: report the existing PR as "already exists" so the
                # performer falls back to its lookup path (no duplicate mint).
                return web.json_response(
                    {"errors": [{"message": f"A pull request already exists for {head}."}]},
                    status=422,
                )
            card_id = self.head_ref_index.get(head)
            if not card_id:
                # 151 review fix: this used to fall through to the 500 below, but
                # performer/github.py:211 only special-cases 422 and raises
                # GitHubAPIError on anything else — so an unindexed head (a
                # rebase-created or retitled branch) killed the dispatch with an
                # opaque 500. 422 is the status GitHub itself returns for a bad
                # head and the one the performer knows how to handle. Deliberately
                # NOT falling back to "the only seeded card": that would mask the
                # very keying bug T025 guards against.
                logger.warning(
                    "fake_github_server.head_not_indexed",
                    head=head, known=sorted(self.head_ref_index),
                )
                return web.json_response(
                    {"message": f"no seeded card for head branch {head!r}",
                     "errors": [{"message": f"Validation failed: unknown head {head!r}"}]},
                    status=422,
                )
            pr_id = self._fake.open_pr(issue_item_id=card_id, head_ref=head, base_ref=base)
            pr = self._fake._prs[pr_id]
        except Exception as exc:
            logger.warning("fake_github_server.create_pr_failed", head=head, error=str(exc))
            return web.json_response({"message": f"PR create failed: {exc}"}, status=500)
        return web.json_response({"html_url": pr["url"], "node_id": pr_id}, status=201)

    async def _h_list_pulls(self, request: web.Request) -> web.Response:
        from aiohttp import web
        head_param = request.query.get("head", "")  # "owner:branch"
        branch = head_param.split(":", 1)[-1] if head_param else ""
        out: list[dict[str, Any]] = []
        for pr in self._fake._prs.values():
            if branch and pr["head_ref"] != branch:
                continue
            if pr["merged"]:
                continue
            out.append(
                {"html_url": pr["url"], "node_id": pr["pr_id"], "number": pr["pr_number"]},
            )
        return web.json_response(out)

    async def _h_get_pull(self, request: web.Request) -> web.Response:
        from aiohttp import web
        number = int(request.match_info["number"])
        pr = self._fake._pr_by_number(number)
        if pr is None:
            return web.json_response({"message": "not found"}, status=404)
        sha = await self._fake._rev(pr["head_ref"])
        return web.json_response({"head": {"sha": sha}})

    async def _h_patch_pull(self, request: web.Request) -> web.Response:
        from aiohttp import web
        # Body/title updates are cosmetic in a bench run; ack so the performer's
        # post-422 PATCH succeeds (it ignores failures anyway).
        number = int(request.match_info["number"])
        pr = self._fake._pr_by_number(number)
        if pr is None:
            return web.json_response({"message": "not found"}, status=404)
        return web.json_response({"html_url": pr["url"], "node_id": pr["pr_id"]})

    async def _h_create_review(self, request: web.Request) -> web.Response:
        from aiohttp import web
        # The reviewer/security performer submits its verdict (POST .../reviews).
        # Record it onto the PR so the coordinare's review reads stay consistent
        # (same shape the auto-approver appends), then ack 201.
        number = int(request.match_info["number"])
        pr = self._fake._pr_by_number(number)
        if pr is None:
            return web.json_response({"message": "not found"}, status=404)
        try:
            body = await request.json()
        except Exception:
            body = {}
        event = str(body.get("event") or "COMMENT").upper()
        state = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}.get(
            event, "COMMENTED",
        )
        sha = await self._fake._rev(pr["head_ref"])
        review = {
            "id": f"REV_{pr['pr_id']}_{len(pr['reviews'])}",
            "author_login": "coordinare-reviewer",
            "state": state,
            "body": str(body.get("body") or ""),
            "submitted_at": datetime.now(UTC).isoformat(),
            "comments": [],
            "commit_oid": sha,
        }
        pr["reviews"].append(review)
        return web.json_response(
            {"id": review["id"], "state": state, "body": review["body"]}, status=201,
        )

    async def _h_issue_comments(self, request: web.Request) -> web.Response:
        from aiohttp import web
        # Reviewer/security dedup path reads the existing PR conversation. Serve the
        # raw GitHub wire shape (id / user.login / body) that performer/github.py
        # list_pr_comments parses — NOT FakeGitHubService.get_issue_comments, which
        # returns the coordinare-normalised id/author/body. Both read the same
        # pr["comments"] list, so the two views can never diverge.
        number = int(request.match_info["number"])
        pr = self._fake._pr_by_number(number)
        return web.json_response(list(pr["comments"]) if pr is not None else [])

    async def _h_post_issue_comment(self, request: web.Request) -> web.Response:
        from aiohttp import web
        # Security stage posts advisory findings (post_pr_comment). Record + ack 201.
        number = int(request.match_info["number"])
        try:
            body = await request.json()
        except Exception:
            body = {}
        created = await self._fake.post_comment(
            number, str(body.get("body") or ""), author="coordinare-performer",
        )
        return web.json_response(created, status=201)

    async def _h_check_runs(self, request: web.Request) -> web.Response:
        from aiohttp import web
        ref = request.match_info["ref"]
        pr = await self._pr_for_ref(ref)
        if pr is None:
            # 151 review fix: an EMPTY check_runs list means "pass" to the
            # performer (summarise_check_runs: "or list is empty", github.py:164),
            # so answering an unresolvable ref with [] is a silent false-green CI
            # gate — exactly the fidelity this bench exists to measure. Refs go
            # unresolved routinely (a poll issued before the PR record exists, a
            # sha whose push has not landed, an amended commit), so answer with a
            # synthetic QUEUED run: the performer reads "pending" and polls again
            # instead of either merging on a green that never ran or crashing on a
            # GitHubAPIError from a non-2xx.
            return web.json_response(
                {"check_runs": [{"name": "pytest", "status": "queued", "conclusion": None, "id": 1}]},
            )
        rollup = await self._fake._ci_rollup(pr)
        runs = [
            {
                "name": c.name,
                "status": c.status,
                "conclusion": c.conclusion,
                "id": i + 1,
            }
            for i, c in enumerate(rollup.checks)
        ]
        return web.json_response({"check_runs": runs})

    async def _h_graphql(self, _request: web.Request) -> web.Response:
        from aiohttp import web
        # Best-effort: report no review threads (resolve_pr_review_threads → 0).
        # The only hard requirement is this call never reaches api.github.com.
        return web.json_response(
            {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}},
        )

    # ---- resolution helpers ----------------------------------------------

    def _pr_for_head(self, head: str) -> dict[str, Any] | None:
        for pr in self._fake._prs.values():
            if pr["head_ref"] == head and not pr["merged"]:
                return pr
        return None

    async def _pr_for_ref(self, ref: str) -> dict[str, Any] | None:
        for pr in self._fake._prs.values():
            if pr["head_ref"] == ref:
                return pr
            if await self._fake._rev(pr["head_ref"]) == ref:
                return pr
        return None
