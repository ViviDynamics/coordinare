"""402: an in-process fake of Scrutare's performer API, served through httpx.MockTransport.

It follows ``docs/app.md`` in ViviDynamics/scrutare: ``POST /api/reviews`` is idempotent
on ``request_id`` and answers 409 when a request id is reused for another target;
``GET /api/jobs/<id>`` returns the job record; ``/healthz`` needs no credential. Tests
drive a job's lifecycle with :meth:`FakeScrutare.finish`.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

TOKEN = "fake-scrutare-token"
LOGIN = "vivi-scrutare[bot]"


@dataclass
class FakeScrutare:
    token: str = TOKEN
    up: bool = True
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_request: dict[str, str] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    @property
    def created(self) -> int:
        return len(self.jobs)

    def job_for(self, request_id: str) -> dict[str, Any]:
        return self.jobs[self.by_request[request_id]]

    def finish(self, job_id: str, status: str, result: dict[str, Any] | None) -> None:
        self.jobs[job_id]["status"] = status
        self.jobs[job_id]["result"] = result

    def post(self, job_id: str, *, verdict: str, head: str, body: str = "summary") -> None:
        self.finish(job_id, "posted", posted_result(verdict=verdict, head=head, body=body))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.up:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        if request.method == "GET" and path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "invalid API credential"})
        if request.method == "POST" and path == "/api/reviews":
            doc = json.loads(request.content)
            if set(doc) - {"repository", "number", "installation", "request_id"}:
                return httpx.Response(400, json={"error": "invalid review request"})
            target = (doc["repository"], doc["number"], doc["installation"])
            known = self.by_request.get(doc["request_id"])
            if known is not None:
                job = self.jobs[known]
                if (job["repository"], job["number"], job["installation"]) != target:
                    return httpx.Response(409, json={"error": "delivery identity conflicts with prior target"})
                return httpx.Response(202, json=job)
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {
                "id": job_id, "delivery": "api:" + doc["request_id"],
                "repository": doc["repository"], "number": doc["number"],
                "installation": doc["installation"], "status": "queued", "result": None,
                "head": None, "reviewed": None, "preflight": [],
            }
            self.by_request[doc["request_id"]] = job_id
            return httpx.Response(202, json=self.jobs[job_id])
        if request.method == "GET" and path.startswith("/api/jobs/"):
            job = self.jobs.get(path.removeprefix("/api/jobs/"))
            if job is None:
                return httpx.Response(404, json={"error": "job not found"})
            return httpx.Response(200, json=job)
        return httpx.Response(404, json={"error": "route not found"})


def posted_result(*, verdict: str, head: str, body: str = "summary", login: str = LOGIN) -> dict[str, Any]:
    """A CLI result document as a posted job carries it (scrutare engine/review.py)."""
    return {
        "schema_version": 1, "scrutare_version": "2026.11.0", "status": "posted",
        "run_dir": "/data/repos/x/.scrutare/runs/r1", "head_sha": head, "verdict": verdict,
        "rule": "code", "panel_status": "complete", "usage": {}, "accounting_complete": True,
        "cost": {"usd": None, "source": "unknown"},
        "review": {
            "review_id": 4242, "html_url": "https://github.com/acme/widgets/pull/7#pullrequestreview-4242",
            "commit_id": head, "body": body, "state": "COMMENTED", "login": login,
        },
    }
